from __future__ import annotations

"""Exact Git integration and durable closeout for the four delivery objects."""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from agent_run.change_delivery_contracts import ChangeJobStopped
from agent_run.change_delivery_state import (
    ChangeDeliveryStateStore, block_change_job, save_change_job,
)
from agent_run.delivery_budget import repair_integration_budget_owner
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitError, GitRepository
from agent_run.integration_resync_budget import (
    complete_integration_window,
    observe_integration_snapshot,
)
from agent_run.semantic_attempt import close_semantic_attempt, pending_semantic_attempt
from agent_run.state import StateStore
from agent_run.work_projection import materialize_history


PARENT_INTEGRATION_COMMIT_MESSAGE = "chore: resolve Parent integration conflict"
_Scope = Literal["ticket", "parent-only", "repair", "run"]


def parent_conflict_commit_boundary(conflict: dict[str, Any]) -> tuple[str, str | None]:
    """Use the saved closeout checkpoint for a follow-up on a resolved stage."""
    if conflict["stage"] in conflict["resolved_stages"]:
        return str(conflict["resolution_sha"]), None
    return str(conflict["head_sha"]), conflict["merge_head_sha"]


class ParentHistoryRewritten(GitError):
    def __init__(self, branch_kind: str, previous: str, observed: str) -> None:
        super().__init__(f"{branch_kind} history changed from {previous} to {observed}")
        self.branch_kind = branch_kind
        self.previous = previous
        self.observed = observed


@dataclass(frozen=True)
class ParentSnapshotResult:
    status: str
    record: dict[str, Any] | None = None
    conflict: dict[str, Any] | None = None


def prepare_ticket_snapshot(
    *, git: GitRepository, github: GitHubPublisher,
    state_store: ChangeDeliveryStateStore, state: dict[str, Any],
    job: dict[str, Any], checkout: Path,
) -> ParentSnapshotResult:
    return _prepare_snapshot(git, github, state_store, state, job, checkout, "ticket")


def prepare_parent_snapshot(
    *, git: GitRepository, github: GitHubPublisher,
    state_store: ChangeDeliveryStateStore, state: dict[str, Any],
    job: dict[str, Any], checkout: Path,
) -> ParentSnapshotResult:
    return _prepare_snapshot(git, github, state_store, state, job, checkout, "parent-only")


def prepare_repair_snapshot(
    *, git: GitRepository, github: GitHubPublisher,
    state_store: ChangeDeliveryStateStore, state: dict[str, Any],
    job: dict[str, Any], checkout: Path,
) -> ParentSnapshotResult:
    return _prepare_snapshot(git, github, state_store, state, job, checkout, "repair")


def prepare_run_snapshot(
    *, git: GitRepository, github: GitHubPublisher, states: StateStore,
    state: dict[str, Any], run: dict[str, Any], checkout: Path,
) -> ParentSnapshotResult:
    return _prepare_snapshot(git, github, states, state, run, checkout, "run")


def finish_ticket_snapshot(
    *, git: GitRepository, github: GitHubPublisher,
    state_store: ChangeDeliveryStateStore, state: dict[str, Any],
    job: dict[str, Any], checkout: Path,
) -> ParentSnapshotResult:
    return _finish_snapshot(git, github, state_store, state, job, checkout, "ticket")


def finish_parent_snapshot(
    *, git: GitRepository, github: GitHubPublisher,
    state_store: ChangeDeliveryStateStore, state: dict[str, Any],
    job: dict[str, Any], checkout: Path,
) -> ParentSnapshotResult:
    return _finish_snapshot(git, github, state_store, state, job, checkout, "parent-only")


def finish_repair_snapshot(
    *, git: GitRepository, github: GitHubPublisher,
    state_store: ChangeDeliveryStateStore, state: dict[str, Any],
    job: dict[str, Any], checkout: Path,
) -> ParentSnapshotResult:
    return _finish_snapshot(git, github, state_store, state, job, checkout, "repair")


def finish_run_snapshot(
    *, git: GitRepository, github: GitHubPublisher, states: StateStore,
    state: dict[str, Any], run: dict[str, Any], checkout: Path,
) -> ParentSnapshotResult:
    return _finish_snapshot(git, github, states, state, run, checkout, "run")


def _record_keys(scope: _Scope) -> tuple[str, str]:
    if scope == "ticket":
        return "ticket_integration", "integration_conflict"
    if scope == "parent-only":
        return "parent_integration", "parent_integration_conflict"
    if scope == "repair":
        return "run_repair_integration", "run_repair_integration_conflict"
    return "run_integration", "run_integration_conflict"


def _save_snapshot(
    store: ChangeDeliveryStateStore | StateStore, state: dict[str, Any],
    subject: dict[str, Any], scope: _Scope,
) -> None:
    if scope == "run":
        cast(StateStore, store).save_run(str(state["run_id"]), state)
        return
    label = (
        f"ticket-{subject['ticket_number']}" if scope == "ticket"
        else "parent-only" if scope == "parent-only"
        else f"run-repair-{subject['repair_attempt']}"
    )
    save_change_job(cast(ChangeDeliveryStateStore, store), state, subject,
                    label=label, development_only=scope == "repair")


def _budget_owner(
    state: dict[str, Any], subject: dict[str, Any], scope: _Scope,
) -> dict[str, Any]:
    return repair_integration_budget_owner(state, subject) if scope == "repair" else subject


def _prepare_snapshot(
    git: GitRepository, github: GitHubPublisher,
    store: ChangeDeliveryStateStore | StateStore, state: dict[str, Any],
    subject: dict[str, Any], checkout: Path, scope: _Scope,
) -> ParentSnapshotResult:
    record_key, conflict_key = _record_keys(scope)
    if scope == "ticket":
        source_branch, target_branch = str(subject["ticket_branch"]), str(state["run_branch"])
    elif scope == "parent-only":
        source_branch, target_branch = str(subject["parent_branch"]), str(state["base"]["branch"])
    elif scope == "repair":
        source_branch, target_branch = str(subject["repair_branch"]), str(state["run_branch"])
    else:
        source_branch, target_branch = str(state["run_branch"]), str(state["base"]["branch"])
    candidate = git.resolve(source_branch) if scope == "run" else str(subject["candidate_sha"])
    confirmed_source = _published_run_head(state, git) if scope == "run" else subject.get("published_sha")
    own_run_candidate = _own_run_ref_write(state, candidate) if scope == "run" else False
    source, target = github.live_branch_heads(source_branch, target_branch)
    if scope == "repair":
        # Repair's source is its local base, independently of its Candidate.
        source = str(subject["base_sha"])
    if source is None:
        raise GitError("Ticket source ref disappeared before integration" if scope == "ticket"
                       else f"remote source ref {source_branch!r} is missing")
    try:
        if scope != "repair":
            git.fetch_remote_commit(source_branch, source)
        git.fetch_remote_commit(target_branch, target)
    except GitError as error:
        error.evidence.update(source_sha=source, target_sha=target)
        if scope != "ticket":
            error.evidence["candidate_sha"] = candidate
        if scope in {"repair", "run"}:
            raise
        return _prepare_failure(git, store, state, subject, checkout, scope, error, source, target)

    prior = subject.get(record_key)
    pending = subject.get(conflict_key)
    if scope == "ticket":
        previous_source = (
            pending.get("source_sha") if isinstance(pending, dict) else None
        ) or subject.get("published_sha") or (
            prior.get("source_sha") if isinstance(prior, dict) else subject["base_sha"]
        )
        previous_target = (
            pending.get("target_sha") if isinstance(pending, dict) else None
        ) or (prior.get("target_sha") if isinstance(prior, dict) else subject["base_sha"])
    else:
        previous_source = prior["source_sha"] if isinstance(prior, dict) else subject.get("published_sha") or subject["base_sha"]
        previous_target = prior["target_sha"] if isinstance(prior, dict) else subject["base_sha"]
        if isinstance(confirmed_source, str) and not git.is_ancestor(confirmed_source, source):
            raise ParentHistoryRewritten("source", confirmed_source, source)
    if not git.is_ancestor(str(previous_source), source):
        raise ParentHistoryRewritten("source", str(previous_source), source)
    if not git.is_ancestor(str(previous_target), target):
        raise ParentHistoryRewritten("target", str(previous_target), target)
    if isinstance(pending, dict):
        if scope != "ticket":
            return ParentSnapshotResult(status="conflict", conflict=pending)
        # A newer pair may prove append-only history, but cannot replace work
        # already paid for against this exact scene.
        source, target = str(pending["source_sha"]), str(pending["target_sha"])

    if scope == "ticket":
        own_source = own_ticket_source_write(subject, source)
        candidate_matches = isinstance(prior, dict) and (
            prior.get("candidate_sha") == candidate
            or (subject.get("attempt_kind") == "ci_repair" and prior.get("head_sha") == candidate)
        )
        source_matches = isinstance(prior, dict) and (
            prior.get("source_sha") == source
            or (own_source and source == subject.get("publication_sha"))
        )
    else:
        intent = subject.get("ticket_write_intent")
        own_head = (
            intent.get("head_sha")
            if isinstance(intent, dict) and intent.get("action") == "publish_ticket_ref"
            else subject.get("published_sha")
        )
        if scope == "repair":
            own_source = True
        elif scope == "run":
            own_source = source == candidate and own_run_candidate
        else:
            own_source = source == own_head
        candidate_matches = isinstance(prior, dict) and candidate in {prior.get("candidate_sha"), prior.get("head_sha")}
        source_matches = isinstance(prior, dict) and (prior.get("source_sha") == source or own_source)
    if isinstance(prior, dict) and candidate_matches and source_matches and prior.get("target_sha") == target:
        if isinstance(pending, dict):
            return ParentSnapshotResult(status="conflict", conflict=pending)
        # Ticket's existing local record validator remains at its caller gate.
        return ParentSnapshotResult(status="clean", record=prior)
    if scope == "ticket" and isinstance(prior, dict):
        from agent_run.change_delivery_state import invalidate_ticket_publication
        # own_source was computed before retiring the old write intent.
        invalidate_ticket_publication(cast(ChangeDeliveryStateStore, store), state, subject)
    observe_integration_snapshot(
        _budget_owner(state, subject, scope), source_sha=source, target_sha=target,
        policy_snapshot=subject.get("policy_snapshot") or state["policy_snapshot"],
        own_source_write=own_source,
    )
    _save_snapshot(store, state, subject, scope)
    start = candidate
    if isinstance(prior, dict) and (
        prior.get("candidate_sha") == candidate if scope == "ticket"
        else git.is_ancestor(candidate, str(prior["head_sha"]))
    ):
        start = str(prior["head_sha"])
    paths = list(prior.get("conflict_paths", [])) if (
        scope == "ticket" and isinstance(prior, dict) and prior.get("candidate_sha") == candidate
    ) else []
    if isinstance(pending, dict):
        if "resolution_sha" not in pending:
            return ParentSnapshotResult(status="conflict", conflict=pending)
        if git.checkout_head(checkout) != pending["resolution_sha"]:
            raise GitError("Recorded integration resolution no longer matches checkout")
        start = str(pending["resolution_sha"])
        paths.extend(path for path in pending.get("prior_paths", []) if path not in paths)
        paths.extend(path for path in pending["paths"] if path not in paths)
    elif git.checkout_head(checkout) != start:
        git.seed_managed_checkout(checkout, expected_head=start)
    try:
        result = _merge_snapshot(git, state, subject, checkout, scope, source, target, candidate, start, paths)
    except GitError as error:
        if scope != "ticket":
            error.evidence.update(source_sha=source, target_sha=target, candidate_sha=candidate)
        return _prepare_failure(git, store, state, subject, checkout, scope, error, source, target, paths)
    _save_snapshot(store, state, subject, scope)
    return result


def _prepare_failure(
    git: GitRepository, store: ChangeDeliveryStateStore | StateStore,
    state: dict[str, Any], subject: dict[str, Any], checkout: Path,
    scope: _Scope, error: GitError, source: str, target: str,
    paths: list[str] | None = None,
) -> ParentSnapshotResult:
    _, conflict_key = _record_keys(scope)
    scene = subject.get(conflict_key)
    if scope == "ticket":
        if not isinstance(scene, dict) or "resolution_sha" in scene or paths is not None:
            scene = failure_scene(git, subject, checkout, error, source=source, target=target, paths=paths or [])
    elif scope == "parent-only":
        if isinstance(scene, dict):
            scene["failure"] = dict(error.evidence) | {"error": str(error)}
        else:
            scene = failure_scene(git, subject, checkout, error, source=source, target=target, paths=[])
            if error.evidence.get("operation") == "fetch_remote_commit":
                # Fetch's root HEAD is evidence, not the editable checkout.
                scene["head_sha"] = git.checkout_head(checkout)
                scene["merge_head_sha"] = git._resolve_in_optional(checkout, "MERGE_HEAD")
            scene.update(resolved_stages=[], conflict_paths=[])
    else:
        if not isinstance(error.evidence.get("head_sha"), str):
            raise error
        scene = {
            "source_sha": source, "target_sha": target,
            "candidate_sha": str(error.evidence["candidate_sha"]),
            "stage": "git_check", "head_sha": error.evidence["head_sha"],
            "merge_head_sha": error.evidence.get("merge_head_sha"),
            "paths": [], "conflict_paths": [], "resolved_stages": [],
            "failure": error.evidence | {"error": str(error)},
        }
    assert isinstance(scene, dict)
    subject[conflict_key] = scene
    _save_snapshot(store, state, subject, scope)
    return ParentSnapshotResult(status="conflict", conflict=scene)


def _merge_snapshot(
    git: GitRepository, state: dict[str, Any], subject: dict[str, Any],
    checkout: Path, scope: _Scope, source: str, target: str,
    candidate: str, start: str, paths: list[str],
) -> ParentSnapshotResult:
    """Apply Git to the supplied exact pair, without choosing a save window."""
    record_key, conflict_key = _record_keys(scope)
    merged = git.integrate_remote_heads(checkout, candidate_sha=start, source_sha=source, target_sha=target)
    scene = subject.get(conflict_key)
    if merged.status == "conflict":
        if scope == "ticket":
            scene = {
                "source_sha": source, "target_sha": target, "candidate_sha": candidate,
                "stage": merged.conflict_stage, "head_sha": merged.head_sha,
                "merge_head_sha": merged.merge_head_sha, "paths": list(merged.conflict_paths),
                "prior_paths": list(paths), "attempts": 0, "failure": merged.failure_evidence,
            }
        elif isinstance(scene, dict):
            resolved = scene["resolved_stages"]
            if merged.conflict_stage in resolved or len([stage for stage in resolved if stage != "git_check"]) >= 2:
                raise GitError("Parent integration still conflicts after bounded resolution")
            scene.update(stage=merged.conflict_stage, head_sha=merged.head_sha,
                         merge_head_sha=merged.merge_head_sha, paths=list(merged.conflict_paths),
                         attempts=0, failure=merged.failure_evidence)
            scene.pop("development_summary", None)
            scene["conflict_paths"].extend(path for path in merged.conflict_paths if path not in scene["conflict_paths"])
        else:
            scene = {
                "source_sha": source, "target_sha": target, "candidate_sha": candidate,
                "stage": merged.conflict_stage, "head_sha": merged.head_sha,
                "merge_head_sha": merged.merge_head_sha, "paths": list(merged.conflict_paths),
                "conflict_paths": list(merged.conflict_paths), "resolved_stages": [],
                "failure": merged.failure_evidence,
            }
        subject[conflict_key] = scene
        return ParentSnapshotResult(status="conflict", conflict=scene)
    record = {
        "candidate_sha": candidate, "candidate_tree": git.resolve(f"{candidate}^{{tree}}"),
        "source_sha": source, "target_sha": target, "head_sha": merged.head_sha,
        "head_tree": git.resolve(f"{merged.head_sha}^{{tree}}"), "conflict_paths": list(paths),
    }
    if scope == "ticket":
        record = {"base_sha": str(subject["base_sha"]), **record}
    elif isinstance(scene, dict):
        record["conflict_paths"] = list(scene["conflict_paths"])
    prior = subject.get(record_key)
    if isinstance(prior, dict) and prior != record:
        subject.setdefault(f"{record_key}_history", []).append(prior)
    subject[record_key] = record
    subject.pop(conflict_key, None)
    complete_integration_window(_budget_owner(state, subject, scope))
    return ParentSnapshotResult(status="clean", record=record)


def _close_development(subject: dict[str, Any], scene: dict[str, Any], outcome: str) -> bool:
    attempt = pending_semantic_attempt(subject, role="development")
    if attempt is None:
        return False
    close_semantic_attempt(subject, attempt, outcome=outcome,
                           result={"development_summary": scene.get("development_summary")})
    return True


def _finish_snapshot(
    git: GitRepository, github: GitHubPublisher,
    store: ChangeDeliveryStateStore | StateStore, state: dict[str, Any],
    subject: dict[str, Any], checkout: Path, scope: _Scope,
) -> ParentSnapshotResult:
    _, conflict_key = _record_keys(scope)
    scene = subject.get(conflict_key)
    if not isinstance(scene, dict):
        raise ValueError("Integration has no pending conflict")
    source, target = str(scene["source_sha"]), str(scene["target_sha"])
    candidate = str(scene["candidate_sha"])
    completed = deepcopy(scene)
    if scope == "ticket":
        paths = list(scene.get("prior_paths", []))
        paths.extend(path for path in scene["paths"] if path not in paths)
        if isinstance(scene.get("resolution_sha"), str):
            head = str(scene["resolution_sha"])
            if git.checkout_head(checkout) != head:
                raise GitError("Recorded integration resolution no longer matches checkout")
        else:
            try:
                head = git.commit_integration_work(
                    checkout, expected_head_sha=str(scene["head_sha"]),
                    expected_merge_sha=scene["merge_head_sha"],
                    message=f"chore(ticket-{subject['ticket_number']}): resolve integration conflict",
                )
            except GitError as error:
                _close_development(subject, completed, "integration_git_check_failed")
                subject.setdefault("integration_work_history", []).append(completed)
                observed_head, observed_merge = error.evidence.get("head_sha"), error.evidence.get("merge_head_sha")
                if observed_head is not None and (observed_head != scene["head_sha"] or observed_merge != scene["merge_head_sha"]):
                    scene["failure"] = error.evidence | {"error": str(error)}
                    block_change_job(
                        cast(ChangeDeliveryStateStore, store), state, subject,
                        label=f"ticket-{subject['ticket_number']}", development_only=False,
                        code="integration_record_invalid", message=str(error),
                    )
                    _save_snapshot(store, state, subject, scope)
                    raise ChangeJobStopped() from error
                subject[conflict_key] = failure_scene(git, subject, checkout, error, source=source, target=target, paths=paths)
                _save_snapshot(store, state, subject, scope)
                return ParentSnapshotResult(status="conflict", conflict=subject[conflict_key])
            _close_development(subject, completed, "integration_conflict_resolved")
            scene["resolution_sha"] = head
            subject.setdefault("integration_work_history", []).append(deepcopy(scene))
            # Ticket's checkpoint has always closed its Attempt before the
            # following integration. A reload must not inherit pending work.
            _save_snapshot(store, state, subject, scope)
        failure = scene.get("failure")
        if isinstance(failure, dict) and failure.get("operation") == "fetch_remote_commit":
            # Preserve the old resolve -> outer observation recovery point.
            return _prepare_snapshot(git, github, store, state, subject, checkout, scope)
        try:
            result = _merge_snapshot(git, state, subject, checkout, scope, source, target, candidate, head, paths)
        except GitError as error:
            return _prepare_failure(git, store, state, subject, checkout, scope, error, source, target, paths)
        _save_snapshot(store, state, subject, scope)
        return result

    # Repair historically closes the role before attempting Git. Its cached
    # summary with no pending Attempt therefore needs no replacement work.
    if scope == "repair" and _close_development(subject, completed, "integration_conflict_resolved"):
        _save_snapshot(store, state, subject, scope)
    try:
        if scope == "parent-only":
            failure = scene.get("failure")
            if isinstance(failure, dict) and failure.get("operation") == "fetch_remote_commit":
                git.fetch_remote_commit(str(subject["parent_branch"]), source)
                git.fetch_remote_commit(str(state["base"]["branch"]), target)
            observe_integration_snapshot(subject, source_sha=source, target_sha=target,
                                         policy_snapshot=subject["policy_snapshot"])
        head = _commit_parent_resolution(git, store, state, subject, checkout, scope, scene)
        result = _merge_snapshot(git, state, subject, checkout, scope, source, target, candidate, head, [])
    except GitError as error:
        if scope != "repair":
            _close_development(subject, completed, "integration_git_check_failed")
        if scope == "parent-only":
            subject.setdefault("integration_work_history", []).append(completed)
            observed_head, observed_merge = error.evidence.get("head_sha"), error.evidence.get("merge_head_sha")
            checkpoint = (scene.get("stage") in scene.get("resolved_stages", [])
                          and observed_head == scene.get("resolution_sha") and observed_merge is None)
            scene["failure"] = error.evidence | {"error": str(error)}
            if observed_head is not None and not checkpoint and (
                observed_head != scene["head_sha"] or observed_merge != scene["merge_head_sha"]
            ):
                raise
            scene.pop("development_summary", None)
        elif not record_parent_integration_failure(subject, scene, error):
            raise
        _save_snapshot(store, state, subject, scope)
        return ParentSnapshotResult(status="conflict", conflict=scene)
    if scope != "repair":
        _close_development(subject, completed, "integration_conflict_resolved")
        subject.setdefault("integration_work_history", []).append(completed)
    # New scene / final record and the just-finished role share this save.
    _save_snapshot(store, state, subject, scope)
    return result


def _commit_parent_resolution(
    git: GitRepository, store: ChangeDeliveryStateStore | StateStore,
    state: dict[str, Any], subject: dict[str, Any], checkout: Path,
    scope: _Scope, scene: dict[str, Any],
) -> str:
    resolved = list(scene["resolved_stages"])
    stage = str(scene["stage"])
    if stage in resolved:
        head = str(scene["resolution_sha"])
        if not git.is_ancestor(head, git.checkout_head(checkout)):
            raise GitError("Parent conflict resolution checkout changed")
        if scene.get("failure") and scene.get("development_summary"):
            head = git.commit_integration_work(checkout, expected_head_sha=head,
                                               expected_merge_sha=None, message=PARENT_INTEGRATION_COMMIT_MESSAGE)
            scene["resolution_sha"] = head
            _save_snapshot(store, state, subject, scope)
        return head
    if len([item for item in resolved if item != "git_check"]) >= 2:
        raise GitError("Parent integration still conflicts after bounded resolution")
    head = git.checkout_head(checkout)
    if stage == "git_check":
        head = git.commit_integration_work(checkout, expected_head_sha=str(scene["head_sha"]),
                                           expected_merge_sha=scene["merge_head_sha"], message=PARENT_INTEGRATION_COMMIT_MESSAGE)
    elif git.commit_parents(head) != [scene["head_sha"], scene["merge_head_sha"]]:
        head = git.commit_integration_resolution(checkout, expected_head_sha=str(scene["head_sha"]),
                                                 expected_merge_sha=str(scene["merge_head_sha"]), message=PARENT_INTEGRATION_COMMIT_MESSAGE)
    resolved.append(stage)
    scene["resolution_sha"], scene["resolved_stages"] = head, resolved
    _save_snapshot(store, state, subject, scope)
    return head


def failure_scene(
    git: GitRepository, job: dict[str, Any], checkout: Path, error: GitError,
    *, source: str, target: str, paths: list[str],
) -> dict[str, Any]:
    evidence = dict(error.evidence or {})
    head = evidence.get("head_sha") or git.checkout_head(checkout)
    return {
        "source_sha": source, "target_sha": target,
        "candidate_sha": str(job["candidate_sha"]), "stage": "git_check",
        "head_sha": head, "merge_head_sha": evidence.get("merge_head_sha"), "paths": list(paths),
        "attempts": 0, "failure": {**evidence, "error": str(error)}, "checkout": str(checkout),
    }


def record_parent_integration_failure(subject: dict[str, Any], scene: dict[str, Any], error: GitError) -> bool:
    """Keep the failed operation and continue only a provable editable scene."""
    subject.setdefault("integration_work_history", []).append(deepcopy(scene))
    scene["failure"] = error.evidence | {"error": str(error)}
    observed_head, observed_merge = error.evidence.get("head_sha"), error.evidence.get("merge_head_sha")
    if not isinstance(observed_head, str):
        return False
    if scene["stage"] in scene["resolved_stages"]:
        scene.update(stage="git_check", head_sha=observed_head, merge_head_sha=observed_merge)
        scene["resolved_stages"] = [stage for stage in scene["resolved_stages"] if stage != "git_check"]
    elif observed_head != scene["head_sha"] or observed_merge != scene["merge_head_sha"]:
        return False
    scene.pop("development_summary", None)
    scene["attempts"] = 0
    return True


def own_ticket_source_write(job: dict[str, Any], source: str) -> bool:
    intent = job.get("ticket_write_intent")
    return source == job.get("published_sha") or (
        isinstance(intent, dict) and intent.get("action") == "publish_ticket_ref"
        and intent.get("head_sha") == source and source == job.get("publication_sha")
    )


def _own_run_ref_write(state: dict[str, Any], candidate: str) -> bool:
    publication = state.get("run_publication")
    if isinstance(publication, dict) and publication.get("published_head_sha") == candidate:
        return True
    run = state.get("run_acceptance")
    if isinstance(run, dict) and run.get("published_integration_head_sha") == candidate:
        return True
    completed = materialize_history(run, "completed_repair_jobs") if isinstance(run, dict) else None
    return isinstance(completed, list) and any(
        isinstance(job, dict) and job.get("phase") == "completed"
        and job.get("acceptance_state") == "promoted" and job.get("integrated_sha") == candidate
        for job in completed
    )


def _published_run_head(state: dict[str, Any], git: GitRepository) -> str | None:
    publication, run = state.get("run_publication"), state.get("run_acceptance")
    integrated = run.get("published_integration_head_sha") if isinstance(run, dict) else None
    head = publication.get("published_head_sha") if isinstance(publication, dict) else None
    if isinstance(integrated, str) and isinstance(head, str):
        if git.is_ancestor(integrated, head):
            return head
        if not git.is_ancestor(head, integrated):
            raise GitError("Published Run authorities have divergent histories")
    return integrated if isinstance(integrated, str) else head if isinstance(head, str) else None


def parent_integration_record_is_valid(git: GitRepository, job: dict[str, Any]) -> bool:
    record = job.get("parent_integration")
    if not isinstance(record, dict) or set(record) != {
        "candidate_sha", "candidate_tree", "source_sha", "target_sha",
        "head_sha", "head_tree", "conflict_paths",
    }:
        return False
    try:
        return (
            job.get("candidate_sha") == record["head_sha"]
            and job.get("base_sha") == record["target_sha"]
            and record["candidate_tree"] == git.resolve(f"{record['candidate_sha']}^{{tree}}")
            and record["head_tree"] == git.resolve(f"{record['head_sha']}^{{tree}}")
            and git.is_ancestor(record["candidate_sha"], record["head_sha"])
            and git.is_ancestor(record["source_sha"], record["head_sha"])
            and git.is_ancestor(record["target_sha"], record["head_sha"])
            and isinstance(record["conflict_paths"], list)
        )
    except (GitError, KeyError, TypeError, ValueError):
        return False
