from __future__ import annotations

"""Ticket delivery integration after fixed-base Development and Review."""

from pathlib import Path
from typing import Any

from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.git import GitError
from agent_run.integration_resync_budget import (
    ResyncBudgetExhausted,
    complete_integration_window,
    observe_integration_snapshot,
)
from agent_run.semantic_attempt import (
    close_semantic_attempt,
    detach_active_invocation,
    pending_semantic_attempt,
)
from agent_run.required_checks_observation import clear_required_checks_observation


def _pause(stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any], code: str, message: str) -> None:
    stage._block(state, job, code, message)
    state["terminal_kind"] = "waiting_human"
    stage.save(state)


def delivery_candidate_sha(job: dict[str, Any]) -> str:
    record = job.get("ticket_integration")
    if isinstance(record, dict) and record.get("candidate_sha") == job.get("candidate_sha"):
        return str(record["head_sha"])
    return str(job["candidate_sha"])


def delivery_base_sha(job: dict[str, Any]) -> str:
    record = job.get("ticket_integration")
    if isinstance(record, dict) and (
        record.get("candidate_sha") == job.get("candidate_sha")
        or (
            job.get("attempt_kind") == "ci_repair"
            and record.get("head_sha") == job.get("candidate_sha")
        )
    ):
        return str(record["target_sha"])
    return str(job["base_sha"])


def integration_record_is_valid(git: Any, job: dict[str, Any]) -> bool:
    record = job.get("ticket_integration")
    if not isinstance(record, dict) or set(record) != {
        "base_sha", "candidate_sha", "candidate_tree", "source_sha", "target_sha",
        "head_sha", "head_tree", "conflict_paths",
    }:
        return False
    if (
        any(not isinstance(record[key], str) or not record[key] for key in record if key != "conflict_paths")
        or not isinstance(record["conflict_paths"], list)
        or any(not isinstance(path, str) or not path for path in record["conflict_paths"])
    ):
        return False
    try:
        promoted_ci_candidate = (
            job.get("attempt_kind") == "ci_repair"
            and record.get("head_sha") == job.get("candidate_sha")
            and record.get("head_tree") == git.resolve(f"{job['candidate_sha']}^{{tree}}")
        )
        return (
            record.get("base_sha") == job.get("base_sha")
            and (
                promoted_ci_candidate
                or (
                    record.get("candidate_sha") == job.get("candidate_sha")
                    and record.get("candidate_tree") == git.resolve(f"{job['candidate_sha']}^{{tree}}")
                )
            )
            and record.get("candidate_tree") == git.resolve(f"{record['candidate_sha']}^{{tree}}")
            and record.get("head_tree") == git.resolve(f"{record['head_sha']}^{{tree}}")
            and git.is_ancestor(str(record["candidate_sha"]), str(record["head_sha"]))
            and git.is_ancestor(str(job["candidate_sha"]), str(record["head_sha"]))
            and git.is_ancestor(str(record["source_sha"]), str(record["head_sha"]))
            and git.is_ancestor(str(record["target_sha"]), str(record["head_sha"]))
        )
    except (GitError, KeyError, TypeError, ValueError):
        return False


def _own_source_write(job: dict[str, Any], source: str) -> bool:
    intent = job.get("ticket_write_intent")
    return source == job.get("published_sha") or (
        isinstance(intent, dict)
        and intent.get("action") == "publish_ticket_ref"
        and intent.get("head_sha") == source
        and source == job.get("publication_sha")
    )


def reconcile_ticket_integration(stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any]) -> bool:
    """Invalidate a published candidate when a new external snapshot is observed."""

    record = job.get("ticket_integration")
    if not isinstance(record, dict) or not integration_record_is_valid(stage.git, job):
        _pause(stage, state, job, "integration_record_invalid", "Ticket integration boundary is invalid")
        return False
    source, target = stage.github.live_branch_heads(
        stage.contract.branch, stage.contract.base_branch
    )
    if source is None:
        _pause(stage, state, job, "remote_source_missing", "Ticket source ref is missing")
        return False
    if (
        job.get("published_sha") == job.get("publication_sha")
        and isinstance(job.get("published_sha"), str)
        and source != job["publication_sha"]
    ):
        _invalidate_ticket_publication(stage, state, job)
        return False
    own_source = _own_source_write(job, source) and source == job.get("publication_sha")
    if (source == record["source_sha"] or own_source) and target == record["target_sha"]:
        return True
    _invalidate_ticket_publication(stage, state, job)
    return False


def _invalidate_ticket_publication(
    stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any]
) -> None:
    pending = pending_semantic_attempt(job, role="publication")
    if pending is not None:
        close_semantic_attempt(job, pending, outcome="currentness_invalidated")
        detach_active_invocation(state, pending)
    ci_repair = job.get("attempt_kind") == "ci_repair"
    job["phase"] = "candidate" if ci_repair else "accepted"
    if ci_repair:
        pending_review = pending_semantic_attempt(job, role="reviewer")
        if pending_review is not None:
            close_semantic_attempt(
                job, pending_review, outcome="currentness_invalidated"
            )
            detach_active_invocation(state, pending_review)
        for key in (
            "acceptance_record", "acceptance_artifact", "publication_authority",
            "ci_repair_publication_receipt", "fallback_publication_receipt",
            "pending_review_result",
        ):
            job.pop(key, None)
    for key in (
        "publication", "publication_sha", "publication_parent_sha", "publication_prior_published_sha",
        "pending_publication_result", "merge_intent", "deterministic_integration_record",
        "ticket_write_intent",
    ):
        job.pop(key, None)
    clear_required_checks_observation(job)
    state["status"] = "ticket_delivery_pending"
    state["diagnostics"] = []
    stage.save(state)


def _observe(stage: ChangeDeliveryStage, job: dict[str, Any]) -> tuple[str, str]:
    source, target = stage.github.live_branch_heads(
        stage.contract.branch, stage.contract.base_branch
    )
    if source is None:
        raise GitError("Ticket source ref disappeared before integration")
    # Exact objects, not local tracking refs, are the only merge inputs.
    try:
        stage.git.fetch_remote_commit(stage.contract.branch, source)
        stage.git.fetch_remote_commit(stage.contract.base_branch, target)
    except GitError as error:
        error.evidence.update(source_sha=source, target_sha=target)
        raise
    old = job.get("ticket_integration")
    conflict = job.get("integration_conflict")
    old_source = (
        conflict.get("source_sha") if isinstance(conflict, dict) else None
    ) or job.get("published_sha") or (
        old.get("source_sha") if isinstance(old, dict) else job["base_sha"]
    )
    old_target = (
        conflict.get("target_sha") if isinstance(conflict, dict) else None
    ) or (old.get("target_sha") if isinstance(old, dict) else job["base_sha"])
    if not stage.git.is_ancestor(str(old_source), source):
        raise HistoryRewritten("source", str(old_source), source)
    if not stage.git.is_ancestor(str(old_target), target):
        raise HistoryRewritten("target", str(old_target), target)
    return source, target


class HistoryRewritten(GitError):
    def __init__(self, branch_kind: str, previous: str, observed: str) -> None:
        super().__init__(f"{branch_kind} history changed from {previous} to {observed}")
        self.branch_kind = branch_kind
        self.previous = previous
        self.observed = observed


def prepare_ticket_integration(
    stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any], checkout: Path
) -> bool:
    """Return True only when one exact live snapshot has been integrated."""

    while True:
        ready = _prepare_ticket_integration_once(stage, state, job, checkout)
        if ready is not None:
            return ready


def _prepare_ticket_integration_once(
    stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any], checkout: Path,
) -> bool | None:
    try:
        source, target = _observe(stage, job)
    except HistoryRewritten as error:
        job["integration_history_rewrite"] = {
            "branch": error.branch_kind,
            "previous_sha": error.previous,
            "observed_sha": error.observed,
        }
        _pause(stage, state, job, "integration_history_rewritten", str(error))
        return False
    except GitError as error:
        observed_source = error.evidence.get("source_sha")
        observed_target = error.evidence.get("target_sha")
        if not isinstance(observed_source, str) or not isinstance(observed_target, str):
            # No reliable snapshot or executable scene can be handed to work.
            raise
        source, target = observed_source, observed_target
        from agent_run.integration_development import failure_scene
        existing = job.get("integration_conflict")
        if not isinstance(existing, dict) or "resolution_sha" in existing:
            job["integration_conflict"] = failure_scene(
                stage, job, checkout, error, source=source, target=target, paths=[],
            )
            stage.save(state)
        if not _resolve_conflict(stage, state, job, checkout):
            return False
        return None
    pending_conflict = job.get("integration_conflict")
    if isinstance(pending_conflict, dict):
        # Finish the recorded conflict scene first. A newer append is picked
        # up by the final live read, then charged as a later resync window.
        source = str(pending_conflict["source_sha"])
        target = str(pending_conflict["target_sha"])
    prior = job.get("ticket_integration")
    own_source_write = _own_source_write(job, source)
    if (
        isinstance(prior, dict)
        and (
            prior.get("candidate_sha") == job.get("candidate_sha")
            or (
                job.get("attempt_kind") == "ci_repair"
                and prior.get("head_sha") == job.get("candidate_sha")
            )
        )
        and (prior.get("source_sha") == source or (
            own_source_write and source == job.get("publication_sha")
        ))
        and prior.get("target_sha") == target
    ):
        if not integration_record_is_valid(stage.git, job):
            _pause(stage, state, job, "integration_record_invalid", "Ticket integration boundary is invalid")
            return False
        return True
    if isinstance(prior, dict):
        _invalidate_ticket_publication(stage, state, job)
    try:
        observe_integration_snapshot(
            job,
            source_sha=source,
            target_sha=target,
            policy_snapshot=job["policy_snapshot"],
            own_source_write=own_source_write,
        )
    except ResyncBudgetExhausted:
        _pause(
            stage, state, job, "integration_resync_exhausted",
            "Ticket integration resync allowance is exhausted; resume explicitly to renew it",
        )
        return False
    stage.save(state)
    start = str(job["candidate_sha"])
    if isinstance(prior, dict) and prior.get("candidate_sha") == start:
        start = str(prior["head_sha"])
    conflict_paths: list[str] = (
        list(prior.get("conflict_paths", []))
        if isinstance(prior, dict) and prior.get("candidate_sha") == job.get("candidate_sha")
        else []
    )
    if not isinstance(pending_conflict, dict):
        if stage.git.checkout_head(checkout) != start:
            stage.git.seed_managed_checkout(checkout, expected_head=start)
    while True:
        scene = job.get("integration_conflict")
        if isinstance(scene, dict):
            conflict_paths.extend(path for path in scene["paths"] if path not in conflict_paths)
            if not _resolve_conflict(stage, state, job, checkout):
                return False
            start = stage.git.checkout_head(checkout)
        try:
            result = stage.git.integrate_remote_heads(
                checkout, candidate_sha=start, source_sha=source, target_sha=target,
            )
        except GitError as error:
            from agent_run.integration_development import failure_scene
            job["integration_conflict"] = failure_scene(
                stage, job, checkout, error, source=source, target=target,
                paths=conflict_paths,
            )
            stage.save(state)
            continue
        if result.status != "conflict":
            break
        job["integration_conflict"] = {
            "source_sha": source, "target_sha": target,
            "candidate_sha": str(job["candidate_sha"]),
            "stage": result.conflict_stage, "head_sha": result.head_sha,
            "merge_head_sha": result.merge_head_sha,
            "paths": list(result.conflict_paths), "prior_paths": list(conflict_paths),
            "attempts": 0,
            "failure": result.failure_evidence,
        }
        stage.save(state)
    record = {
        "base_sha": str(job["base_sha"]),
        "candidate_sha": str(job["candidate_sha"]),
        "candidate_tree": stage.git.resolve(f"{job['candidate_sha']}^{{tree}}"),
        "source_sha": source,
        "target_sha": target,
        "head_sha": result.head_sha,
        "head_tree": stage.git.resolve(f"{result.head_sha}^{{tree}}"),
        "conflict_paths": list(conflict_paths),
    }
    if isinstance(prior, dict) and prior != record:
        job.setdefault("ticket_integration_history", []).append(prior)
    job["ticket_integration"] = record
    job.pop("integration_conflict", None)
    complete_integration_window(job)
    stage.save(state)
    return reconcile_ticket_integration(stage, state, job)


def _resolve_conflict(stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> bool:
    from agent_run.integration_development import resolve_integration_work
    return resolve_integration_work(stage, state, job, checkout)
