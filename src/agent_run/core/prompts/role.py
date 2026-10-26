"""Render runtime and read-only previews from the same role facts and resources."""

from __future__ import annotations

from typing import Any

from agent_run.core.prompts.development import development_prompt
from agent_run.contracts.artifact import role_schema
from agent_run.contracts.copy import text, validate_language
from agent_run.core.prompts.context import pretty, structured_output_repair_prompt
from agent_run.contracts.prompt import validate_resources
from agent_run.core.prompts.publication import publication_continuation_prompt, publication_prompt
from agent_run.core.prompts.review import review_continuation_prompt, review_prompt
from agent_run.config.resources import selected_language

from agent_run.model import AgentCall, AgentPreparation, DeliveryKind, Purpose, Role
from agent_run.errors import KernelError


_OUTPUT_ROLES = {"Development result": "development", "Acceptance Artifact": "review",
                 "Publication Artifact": "publication"}


def prompt_schema(role: str, request: dict[str, Any]) -> dict[str, Any]:
    """The independently transmitted backend schema, never copied into prose."""
    if role == "output-repair":
        name = request.get("output_name")
        if not isinstance(name, str) or name not in _OUTPUT_ROLES:
            raise ValueError("unknown structured output role")
        role = _OUTPUT_ROLES[name]
    return role_schema("publication" if role == "final-publication" else role)


def render_prompt(request: dict[str, Any], role: str, *, continuation: bool = False) -> str:
    """Shared assembly only: no settings writes, lifecycle or model execution."""
    request = dict(request)
    if role == "output-repair":
        name, error = request.get("output_name"), request.get("contract_error")
        language = selected_language(request.get("language"))
        if not isinstance(name, str) or not isinstance(error, str):
            raise ValueError(text("prompts.repair_fields", language=language))
        if continuation:
            raise ValueError(text("prompts.repair_continuation", language=language))
        return structured_output_repair_prompt(name, error, request=request)
    if "human_responses" in request:
        responses = request["human_responses"]
        if not isinstance(responses, list) or any(not isinstance(value, str) for value in responses):
            raise KernelError("prompt_human_responses_invalid")
        request["human_response_history"] = [{"response": value} for value in responses]
    if role == "development":
        if isinstance(request.get("integration_evidence"), dict):
            request["integration_evidence"] = pretty(request["integration_evidence"])
        return development_prompt(request, force_continuation=continuation)
    if role == "review":
        # Unbound method inspection is a read-only public preview. Allocated
        # Workers must supply their frozen snapshot before reaching assembly.
        if "review_context" not in request:
            return (review_continuation_prompt if continuation else review_prompt)(request)
        review = request.get("review_context")
        if (not isinstance(review, dict) or not isinstance(review.get("facts"), dict)
                or not isinstance(review.get("review_range"), dict)):
            raise KernelError("review_context_unavailable")
        if (not isinstance(review["facts"].get("current_review_identity"), dict)
                or not review["facts"]["current_review_identity"]
                or any(not isinstance(review["review_range"].get(field), str) or not review["review_range"][field]
                       for field in ("base_sha", "candidate_sha"))
                or review["review_range"]["candidate_sha"] != request.get("reviewed_commit")):
            raise KernelError("review_context_identity_unavailable")
        # The saved facts and comparison are shared by execution and preview.
        for key in ("_prompt_resources", "language", "_invocation_mode", "thread_id", "acceptance_scope",
                    "work_source", "parent_issue_url", "task_issue_url", "checkout"):
            if key in review["facts"] and review["facts"][key] != request.get(key):
                raise KernelError("review_prompt_binding", key)
        request.update(review["facts"])
        return (review_continuation_prompt if continuation else review_prompt)(request)
    if role in {"publication", "final-publication"}:
        if sum(isinstance(request.get(key), dict) for key in (
            "acceptance_artifact", "fallback_publication_context", "ci_repair_publication_context",
        )) != 1:
            raise KernelError("publication_evidence_unavailable")
        build = publication_continuation_prompt if continuation else publication_prompt
        return build(request, final_run=role == "final-publication")
    raise KernelError("prompt_role")


def render_role_prompt(call: AgentCall | AgentPreparation) -> str:
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
    if call.output_ordinal > 0:
        error = context.get("contract_error")
        if context.get("readonly") is not True or not isinstance(error, str) or not error:
            raise KernelError("output_repair_prompt_unavailable")
        request["output_name"] = next(name for name, role in _OUTPUT_ROLES.items() if role == call.role.value)
        return render_prompt(request, "output-repair")
    continuation = context.get("semantic_continuation")
    if type(continuation) is not bool:
        raise KernelError("prompt_continuation_unavailable")
    request["_invocation_mode"] = ("new-thread" if call.mode == "fresh" else
                                   "resume" if continuation else "new-attempt")
    scope = DeliveryKind(context["scope"])
    request.update(
        acceptance_scope="run" if scope == DeliveryKind.FINAL_RUN else scope.value,
        parent_issue_url=f"https://github.com/{context['repository']}/issues/{context['parent_issue']}",
        task_issue_url=f"https://github.com/{context['repository']}/issues/{context['issue']}",
        work_source="ci" if call.purpose == Purpose.CI else "ordinary",
    )
    if call.role == Role.REVIEW and not isinstance(context.get("review_context"), dict):
        raise KernelError("review_context_unavailable")
    if call.role == Role.PUBLICATION and not isinstance(context.get("publication_context"), dict):
        raise KernelError("publication_scope_unavailable")
    role = "final-publication" if call.role == Role.PUBLICATION and scope == DeliveryKind.FINAL_RUN else call.role.value
    return render_prompt(request, role)
