"""固定 Reviewer 实际收到的事实；保存在既有 Semantic Attempt 内。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent_run.artifacts import AcceptanceArtifact
from agent_run.git import GitRepository
from agent_run.semantic_attempt import semantic_attempt_subjects
from agent_run.review_evidence import same_review_requirements

_REQUEST_FACTS = (
    "acceptance_scope", "parent_issue_url", "task_issue_url", "parent", "ticket",
    "effective_revision", "ticket_graph", "ticket_completion_records",
    "current_review_identity", "previous_review_identity", "previous_acceptance_artifact",
    "parent_acceptance_baseline", "expected_merge_result", "run_review_facts",
    "integration_record", "run_integration_record", "ci_evidence", "ci_failure_context",
    "work_source", "attempt_kind", "repair_scope", "candidate_acceptance",
    "fallback_ticket_records", "ticket_integration_records", "ci_repair_ticket_records",
)
_BINDING_FACTS = (
    "base_sha", "candidate_sha", "default_base_sha", "effective_revision",
    "parent_revision", "ticket_graph_revision", "ticket_completion_records", "repair_source",
)


def prepare_review_context(
    request: dict[str, Any], attempt: dict[str, Any], job: dict[str, Any],
    git: GitRepository, state: dict[str, Any],
) -> dict[str, Any]:
    """Freeze facts before dispatch, keeping full scope separate from repair focus."""
    # A saved return belongs to the already dispatched snapshot. A later
    # operator message must not retroactively become evidence for that audit.
    if isinstance(attempt.get("returned_result"), dict):
        saved = attempt.get("review_context")
        if not isinstance(saved, dict):
            raise ValueError("Saved Reviewer return is missing its Review Context")
        request["review_context"] = deepcopy(saved)
        return deepcopy(saved)
    facts = deepcopy({key: request[key] for key in _REQUEST_FACTS if key in request})
    if request["acceptance_scope"] == "run":
        for key in ("parent", "ticket_graph"):
            facts[key] = deepcopy(state[key])
        if "ticket_completion_records" not in facts:
            facts["ticket_completion_records"] = deepcopy(request["run_review_facts"]["ticket_completion_records"])
    binding = deepcopy({key: job[key] for key in _BINDING_FACTS if key in job})
    identity = facts["current_review_identity"]
    base = identity.get("reviewed_base_sha", identity.get("default_base_sha"))
    candidate = identity.get("reviewed_candidate_sha", identity.get("repair_candidate_sha", identity.get("run_head_sha")))
    if base is None:
        base = binding["default_base_sha"]
    base, candidate = git.resolve(str(base)), git.resolve(str(candidate))
    previous = facts.get("previous_review_identity", {})
    previous_candidate = previous.get("reviewed_candidate_sha", previous.get("repair_candidate_sha", previous.get("run_head_sha")))
    artifact = facts.get("previous_acceptance_artifact")
    applicable = False
    if isinstance(artifact, dict) and previous_candidate:
        parsed = AcceptanceArtifact.parse(artifact)
        for owner in [job, *semantic_attempt_subjects(state)]:
            for prior in reversed(owner.get("semantic_attempt_history", [])):
                old = prior.get("review_context")
                if not isinstance(old, dict) or prior.get("outcome") != "acceptance_artifact":
                    continue
                if old["full_range"]["candidate_sha"] != previous_candidate:
                    continue
                same_requirements = same_review_requirements(old["facts"], facts)
                applicable = (
                    not any(lane["status"] == "blocked" for lane in parsed.checks.values())
                    and same_requirements
                    and old["full_range"]["base_sha"] == base
                    and git.is_ancestor(str(previous_candidate), candidate)
                )
                break
            if applicable:
                break
    context: dict[str, Any] = {
        "facts": facts,
        "binding": binding,
        "full_range": {"base_sha": base, "candidate_sha": candidate},
        "focus_range": {
            "base_sha": str(previous_candidate) if applicable else base,
            "candidate_sha": candidate,
        },
        "has_complete_baseline": applicable,
    }
    # Same semantic work retains its original evidence on interruption/reload.
    saved = attempt.get("review_context")
    if (
        isinstance(saved, dict)
        and {key: value for key, value in saved.get("binding", {}).items() if key != "repair_source"}
        == {key: value for key, value in binding.items() if key != "repair_source"}
        and saved.get("facts", {}).get("current_review_identity") == identity
        and same_review_requirements(saved.get("facts", {}), facts)
    ):
        context = deepcopy(saved)
    if attempt.get("budget_consumed") is True and isinstance(job.get("acceptance_artifact"), dict):
        artifacts = context["facts"].setdefault("continuation_acceptance_artifacts", [])
        if not artifacts or artifacts[-1] != job["acceptance_artifact"]:
            artifacts.append(deepcopy(job["acceptance_artifact"]))
        # Match the bounded Attempt history while preserving the first original.
        del artifacts[1:-63]
    for key in ("prior_human_blockers", "human_response_history"):
        if key in request:
            context["facts"][key] = deepcopy(request[key])
    attempt["review_context"] = deepcopy(context)
    top_level_requirements = {key for key in ("parent", "ticket_graph", "ticket_completion_records") if key in request}
    for key in _REQUEST_FACTS:
        request.pop(key, None)
    request.update(deepcopy({key: value for key, value in context["facts"].items()
                             if key in top_level_requirements or key not in {"parent", "ticket_graph", "ticket_completion_records"}}))
    request["review_context"] = deepcopy(context)
    return context


def review_context_for_result(job: dict[str, Any]) -> dict[str, Any]:
    attempt = job.get("pending_semantic_attempt")
    context = attempt.get("review_context") if isinstance(attempt, dict) else None
    if not isinstance(context, dict):
        raise ValueError("Reviewer result is missing its dispatched Review Context")
    return context


def review_record_identity(context: dict[str, Any]) -> dict[str, Any]:
    identity = context["facts"]["current_review_identity"]
    if context["facts"]["acceptance_scope"] == "parent_only":
        return {key: identity[key] for key in (
            "reviewed_base_sha", "reviewed_candidate_sha", "reviewed_candidate_tree",
        )}
    return deepcopy(identity)


def run_acceptance_record(context: dict[str, Any], thread_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
    facts = context["facts"]
    identity = facts["current_review_identity"]
    return {
        "acceptance_scope": "run",
        "reviewed_base_sha": identity["default_base_sha"],
        "reviewed_default_base_sha": identity["default_base_sha"],
        "reviewed_head_sha": identity["run_head_sha"],
        "expected_merge_tree": identity["expected_merge_tree"],
        "parent_revision": facts["parent"]["revision"],
        "ticket_graph_revision": facts["ticket_graph"]["revision"],
        "ticket_completion_records": deepcopy(facts["ticket_completion_records"]),
        "reviewer_thread_id": thread_id,
        "artifact": deepcopy(artifact),
    }


def review_context_is_current(job: dict[str, Any]) -> bool:
    attempt = job.get("pending_semantic_attempt")
    context = attempt.get("review_context") if isinstance(attempt, dict) else None
    if not isinstance(context, dict):
        return True  # Other roles have their own existing currentness contracts.
    return all(job.get(key) == value for key, value in context["binding"].items()
               if key != "repair_source")


def candidate_review_identity(job: dict[str, Any], git: GitRepository) -> dict[str, Any]:
    return {
        "reviewed_base_sha": str(job["base_sha"]),
        "reviewed_candidate_sha": str(job["candidate_sha"]),
        "reviewed_candidate_tree": git.resolve(f"{job['candidate_sha']}^{{tree}}"),
    }
