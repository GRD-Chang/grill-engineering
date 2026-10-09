from __future__ import annotations

"""Durable, exact fast-forward delivery of a reviewed Parent repair."""

from typing import Any, TYPE_CHECKING

from agent_run.git import GitError
from agent_run.github import GitHubReadError
from agent_run.run_repair_integration import repair_run_branch_is_current

if TYPE_CHECKING:
    from agent_run.run_acceptance import RunAcceptanceEngine
    from agent_run.run_repair_promotion import RunRepairPromotion


def update_run_branch(
    owner: RunAcceptanceEngine, promotion: RunRepairPromotion,
    state: dict[str, Any], job: dict[str, Any],
) -> bool:
    github = owner.github
    if github is None:
        raise ValueError("Parent repair requires the Publisher")
    intent = job.get("run_branch_write_intent")
    if not isinstance(intent, dict):
        acceptance = job.get("acceptance_record")
        if not isinstance(acceptance, dict) or not owner.candidate_acceptance.is_current(
            state, job, acceptance,
        ):
            promotion.invalidate_stale(state, job, owner._repair_checkout(state))
            owner._save(state)
            return False
        if not owner._refresh_run_currentness(state) or not promotion._repair_trigger_is_current(
            state, job,
        ):
            owner._save(state)
            return False
        if owner._default_head(state) != job["default_base_sha"]:
            promotion.invalidate_stale(state, job, owner._repair_checkout(state))
            owner._save(state)
            return False
        intent = {
            "branch": str(state["run_branch"]),
            "base_sha": str(job["base_sha"]),
            "candidate_sha": str(job["candidate_sha"]),
            "default_base_sha": str(job["default_base_sha"]),
        }
        if not owner.git.is_ancestor(intent["base_sha"], intent["candidate_sha"]):
            raise GitError("Parent repair Candidate does not descend from the Run Branch")
        job["run_branch_write_intent"] = intent
        owner._save(state)
    branch = str(intent["branch"])
    base = str(intent["base_sha"])
    candidate = str(intent["candidate_sha"])
    if (
        branch != state["run_branch"]
        or candidate != job.get("candidate_sha")
        or base != job.get("base_sha")
        or intent["default_base_sha"] != job.get("default_base_sha")
    ):
        raise ValueError("Parent repair write intent does not match its Candidate")
    if not owner.git.is_ancestor(base, candidate):
        raise GitError("Saved Parent repair Candidate lacks its Run Branch ancestor")
    try:
        observed, _default = github.live_branch_heads(branch, str(state["base"]["branch"]))
        if observed == candidate:
            pass
        elif observed == base:
            # An unresolved write is observed before deciding whether the
            # saved evidence still authorizes a write that has not happened.
            acceptance = job.get("acceptance_record")
            if (
                not owner._refresh_run_currentness(state)
                or not isinstance(acceptance, dict)
                or not owner.candidate_acceptance.is_current(state, job, acceptance)
                or not promotion._repair_trigger_is_current(state, job)
            ):
                job.pop("run_branch_write_intent", None)
                promotion.invalidate_stale(state, job, owner._repair_checkout(state))
                owner._save(state)
                return False
            # publish_branch validates ancestry before its compare-and-swap.
            github.publish_branch(branch, candidate, expected_remote_sha=base)
        elif observed != candidate:
            raise GitError("Run Branch no longer matches the saved repair write intent")
        observed, _default = github.live_branch_heads(branch, str(state["base"]["branch"]))
        if observed != candidate:
            raise GitError("Run Branch repair write readback did not match its Candidate")
        local = owner.git.resolve(branch)
        if local != candidate and (
            not repair_run_branch_is_current(owner.git, job, local)
            or not owner.git.is_ancestor(local, candidate)
        ):
            raise GitError("Local Run Branch changed while repair write was pending")
        if local != candidate:
            result = owner.git._run("update-ref", f"refs/heads/{branch}", candidate, local)
            if result.returncode != 0:
                raise GitError(result.stderr.strip() or "Run Branch local compare-and-swap failed")
    except (GitError, GitHubReadError, OSError, TimeoutError) as error:
        job.update({"phase": "blocked", "blocked_reason": "run_branch_write_failed"})
        state.update({
            "status": "ready_for_human", "terminal_kind": "waiting_human",
            "diagnostics": [{"code": "run_branch_write_failed", "message": str(error)}],
        })
        owner._save(state)
        return False
    # Keep the original write intent until promotion is durably saved. A lost
    # response or failed StateStore save therefore resumes through readback.
    job.update({"integrated_sha": candidate, "publication_sha": candidate})
    job.pop("blocked_reason", None)
    job["phase"] = "accepted"
    owner._save(state)
    if not promotion.after_update(state, job):
        owner._save(state)
        return False
    job["phase"] = "completed"
    owner._save(state)
    return True
