"""Render one already allocated role from its frozen resources and facts."""

from __future__ import annotations

from typing import Any

from agent_run.development_prompts import development_prompt
from agent_run.messages import validate_language
from agent_run.prompt_context import pretty, structured_output_repair_prompt
from agent_run.prompt_resources import validate_resources
from agent_run.publication_prompts import publication_prompt
from agent_run.reviewer_prompts import review_prompt

from .model import AgentCall, DeliveryKind, KernelError, Purpose, Role


def render_role_prompt(call: AgentCall) -> str:
    """No current settings, Git reads, state writes or successor decisions."""
    context = call.context
    # Never let the legacy pure renderers' optional resource fallback read a
    # different personal configuration for an already allocated invocation.
    if "_prompt_resources" not in context:
        raise KernelError("prompt_resources_unavailable")
    validate_resources(context["_prompt_resources"])
    validate_language(context["language"])
    if context["role"] != call.role or context["purpose"] != call.purpose:
        raise KernelError("prompt_call_binding")
    if call.mode not in {"fresh", "resume", "output-repair", "execution-recovery"}:
        raise KernelError("prompt_invocation_mode")
    if (call.mode == "fresh" and call.reported_thread is not None
            or call.mode != "fresh" and (not isinstance(call.reported_thread, str) or not call.reported_thread.strip())):
        raise KernelError("prompt_thread_binding")
    request: dict[str, Any] = dict(context)
    request["thread_id"] = call.reported_thread
    if call.mode == "output-repair":
        error = context.get("contract_error")
        if context.get("readonly") is not True or not isinstance(error, str) or not error:
            raise KernelError("output_repair_prompt_unavailable")
        names = {Role.DEVELOPMENT: "Development result", Role.REVIEW: "Acceptance Artifact",
                 Role.PUBLICATION: "Publication Artifact"}
        return structured_output_repair_prompt(names[call.role], error, request=request)
    continuation = context.get("semantic_continuation")
    if type(continuation) is not bool:
        raise KernelError("prompt_continuation_unavailable")
    request["_invocation_mode"] = ("new-thread" if call.mode == "fresh" else
                                   "resume" if continuation else "new-attempt")
    responses = context.get("human_responses", [])
    if not isinstance(responses, list) or any(not isinstance(value, str) for value in responses):
        raise KernelError("prompt_human_responses_invalid")
    request["human_response_history"] = [{"response": value} for value in responses]
    scope = DeliveryKind(context["scope"])
    request.update(
        acceptance_scope="run" if scope == DeliveryKind.FINAL_RUN else scope.value,
        parent_issue_url=f"https://github.com/{context['repository']}/issues/{context['parent_issue']}",
        task_issue_url=f"https://github.com/{context['repository']}/issues/{context['issue']}",
        work_source="ci" if call.purpose == Purpose.CI else "ordinary",
    )
    if call.role == Role.DEVELOPMENT:
        if isinstance(request.get("integration_evidence"), dict):
            request["integration_evidence"] = pretty(request["integration_evidence"])
        return development_prompt(request)
    if call.role == Role.REVIEW:
        review = context.get("review_context")
        if (not isinstance(review, dict) or not isinstance(review.get("facts"), dict)
                or not isinstance(review.get("full_range"), dict)
                or not isinstance(review.get("focus_range"), dict)
                or type(review.get("has_complete_baseline")) is not bool):
            raise KernelError("review_context_unavailable")
        if (not isinstance(review["facts"].get("current_review_identity"), dict)
                or not review["facts"]["current_review_identity"]
                or any(not isinstance(review[key].get(field), str) or not review[key][field]
                       for key in ("full_range", "focus_range") for field in ("base_sha", "candidate_sha"))):
            raise KernelError("review_context_identity_unavailable")
        # These facts were selected and persisted by Kernel, including whether
        # a previous result is an applicable baseline. Rendering cannot infer it.
        for key in ("_prompt_resources", "language", "_invocation_mode", "thread_id", "acceptance_scope",
                    "work_source", "parent_issue_url", "task_issue_url", "checkout"):
            if key in review["facts"] and review["facts"][key] != request.get(key):
                raise KernelError("review_prompt_binding", key)
        request.update(review["facts"])
        return review_prompt(request)
    if call.role == Role.PUBLICATION:
        if sum(isinstance(context.get(key), dict) for key in (
            "acceptance_artifact", "fallback_publication_context", "ci_repair_publication_context",
        )) != 1:
            raise KernelError("publication_evidence_unavailable")
        return publication_prompt(request, final_run=scope == DeliveryKind.FINAL_RUN)
    raise KernelError("prompt_role")
