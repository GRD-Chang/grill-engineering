from __future__ import annotations

"""Ticket delivery integration after fixed-base Development and Review."""

import json
from pathlib import Path
from typing import Any

from agent_run.agents import HumanBlockerResult
from agent_run.artifacts import clear_current_human_blocker
from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.change_delivery_threads import (
    record_development_thread,
    require_development_thread,
    select_development_thread,
)
from agent_run.credential_availability import clear_initial_credential_wait
from agent_run.git import GitError
from agent_run.integration_resync_budget import (
    ResyncBudgetExhausted,
    complete_integration_window,
    observe_integration_snapshot,
)
from agent_run.semantic_attempt import (
    allocate_semantic_attempt,
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
    stage.git.fetch_remote_commit(stage.contract.branch, source)
    stage.git.fetch_remote_commit(stage.contract.base_branch, target)
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
    resolved_stages: set[str] = set()
    if isinstance(pending_conflict, dict):
        if (
            pending_conflict.get("source_sha") != source
            or pending_conflict.get("target_sha") != target
            or pending_conflict.get("candidate_sha") != job.get("candidate_sha")
        ):
            _pause(
                stage, state, job, "integration_snapshot_changed_during_conflict",
                "Remote snapshot changed while Ticket conflict resolution was pending",
            )
            return False
        conflict_paths = list(pending_conflict.get("prior_paths", []))
        conflict_paths.extend(path for path in pending_conflict["paths"] if path not in conflict_paths)
        resolved_stages = set(pending_conflict.get("prior_stages", []))
        if not _resolve_conflict(stage, state, job, checkout):
            return False
        resolved_stages.add(str(pending_conflict["stage"]))
        result = stage.git.integrate_remote_heads(
            checkout, candidate_sha=stage.git.checkout_head(checkout),
            source_sha=source, target_sha=target,
        )
    else:
        if stage.git.checkout_head(checkout) != start:
            stage.git.seed_managed_checkout(checkout, expected_head=start)
        result = stage.git.integrate_remote_heads(
            checkout, candidate_sha=start, source_sha=source, target_sha=target
        )
    while result.status == "conflict":
        if result.conflict_stage in resolved_stages or len(resolved_stages) >= 2:
            _pause(stage, state, job, "integration_conflict_unresolved", "Ticket integration still conflicts")
            return False
        job["integration_conflict"] = {
            "source_sha": source,
            "target_sha": target,
            "candidate_sha": str(job["candidate_sha"]),
            "stage": result.conflict_stage,
            "head_sha": result.head_sha,
            "merge_head_sha": result.merge_head_sha,
            "paths": list(result.conflict_paths),
            "prior_paths": list(conflict_paths),
            "prior_stages": sorted(resolved_stages),
            "attempts": 0,
        }
        stage.save(state)
        if not _resolve_conflict(stage, state, job, checkout):
            return False
        for path in result.conflict_paths:
            if path not in conflict_paths:
                conflict_paths.append(path)
        resolved_stages.add(str(result.conflict_stage))
        result = stage.git.integrate_remote_heads(
            checkout,
            candidate_sha=stage.git.checkout_head(checkout),
            source_sha=source,
            target_sha=target,
        )
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
    conflict = job["integration_conflict"]
    existing_attempt = pending_semantic_attempt(job, role="development")
    if conflict["attempts"] >= 1 and existing_attempt is None and "development_summary" not in conflict:
        _pause(stage, state, job, "integration_conflict_unresolved", "Ticket conflict resolution already attempted")
        return False
    if "development_summary" not in conflict:
        stage.require_agent_work("integration_conflict")
    conflict["attempts"] = 1
    select_development_thread(state, job, new_attempt=existing_attempt is None)
    request = stage.adapter.development_request(state, job, checkout)
    request.update({
        "repair_source": "merge_conflict",
        "merge_conflict_evidence": json.dumps(conflict, ensure_ascii=False, sort_keys=True, indent=2),
        "_prompt_resources": state["prompt_resources"],
    })
    work_subject, generation = stage.adapter.invocation_identity(state, job)
    if existing_attempt is None:
        job["integration_conflict_agent_ordinal"] = max(
            int(job.get("modification_attempts", 0)),
            int(job.get("integration_conflict_agent_ordinal", 0)),
        ) + 1
    attempt = allocate_semantic_attempt(
        job, role="development", work_subject=work_subject, generation=generation,
        currentness_boundary=stage._invocation_boundary(job),
        ordinal=int(job["integration_conflict_agent_ordinal"]),
        budget_window=int(job["review_budget"]["window"]),
    )
    request["_invocation_event"] = stage._invocation_events(
        state, job, request, role="development", phase="integration_conflict",
        semantic_attempt=attempt,
    )
    request["_currentness_check"] = lambda: stage._agent_is_current(state, job)
    if job.get("development_new_thread") is True:
        request["_invocation_mode"] = "new-thread"
    elif job.get("development_failure_resume") is True:
        request["_invocation_mode"] = "resume"
    stage.save(state)
    if "development_summary" not in conflict:
        result = stage.agents.develop(request)
        require_development_thread(job, result.thread_id, requested_thread=request.get("thread_id"))
        clear_initial_credential_wait(state, work_subject=stage.contract.label)
        if not stage.adapter.development_thread_is_allowed(state, result.thread_id):
            raise ValueError("Change Job Development Thread is not independent")
        record_development_thread(job, result.thread_id, result.replaced_thread_id)
        job.pop("development_new_thread", None)
        job.pop("development_failure_resume", None)
        if isinstance(result, HumanBlockerResult):
            stage._wait_for_human(
                state, job,
                phase="candidate" if job.get("attempt_kind") == "ci_repair" else "accepted",
                blockers=result.human_blockers,
                code="agent_requires_human",
            )
            return False
        if not isinstance(result.summary, str) or not result.summary.strip():
            _pause(stage, state, job, "integration_conflict_unresolved", "Conflict Agent returned no result")
            return False
        conflict["development_summary"] = result.summary
        clear_current_human_blocker(job)
        stage.save(state)
        stage._reject_stale(
            state, job, checkout,
            "Ticket conflict resolution was discarded after requirements changed",
        )
    resolved = stage.git.checkout_head(checkout)
    if stage.git.commit_parents(resolved) != [conflict["head_sha"], conflict["merge_head_sha"]]:
        try:
            resolved = stage.git.commit_integration_resolution(
                checkout,
                expected_head_sha=str(conflict["head_sha"]),
                expected_merge_sha=str(conflict["merge_head_sha"]),
                message=f"chore(ticket-{job['ticket_number']}): resolve integration conflict",
            )
        except GitError as error:
            _pause(stage, state, job, "integration_conflict_unresolved", str(error))
            return False
    close_semantic_attempt(job, attempt, outcome="integration_conflict_resolved", result={"development_summary": conflict["development_summary"]})
    conflict["resolution_sha"] = resolved
    stage.save(state)
    return True
