from __future__ import annotations

import inspect
import json
import math
import os
import re
import threading
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from agent_run.agents import (
    DevelopmentResult,
    HumanBlockerResult,
    PublicationResult,
    ReviewResult,
)
from agent_run.agent_schemas import (
    acceptance_schema,
    development_or_human_blocker_schema,
    publication_or_human_blocker_schema,
)
from agent_run.artifacts import (
    AcceptanceArtifact,
    PublicationArtifact,
    parse_development_wire_result,
    parse_human_blockers,
    parse_publication_wire_result,
)
from agent_run.github_auth import _GitHubAppCredentialProvider
from agent_run.github import _repository_hint_from_origin
from agent_run.github_auth_profile import (
    GitHubAppProfileStore,
    load_github_app_profile,
)
from agent_run.error_safety import bounded_error
from agent_run.execution_binding import emit_execution_binding
from agent_run.executor_environment import ensure_executor_runtime_directory
from agent_run.run_locator import RunLocatorIndex
from agent_run.worker_sandbox import (
    WorkerDeadlineExceeded,
    WorkerSandboxError,
    bubblewrap_command,
    create_gh_access_adapter,
    run_worker_process,
    _worker_gh_targets,
    worker_credential_environment,
)
from agent_run.worker_credentials import (
    CredentialProvider,
    HostGitHubReadChannel,
    InitialCredentialUnavailable,
    WorkerCredentialChannel,
    WorkerCredentialError,
)


class CodexProcessError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        return_code: int | None = None,
        signal_number: int | None = None,
        recoverable: bool = True,
        capacity_failure: bool = False,
        machine_error: str | None = None,
        completed_output: bool = False,
    ) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.signal_number = signal_number
        self.recoverable = recoverable
        self.capacity_failure = capacity_failure
        self.machine_error = machine_error
        self.completed_output = completed_output


class _CodexThreadResumeError(CodexProcessError):
    pass


class _CodexInvocationDeadlineError(CodexProcessError):
    def __init__(self, message: str, *, process_started: bool) -> None:
        super().__init__(message)
        self.process_started = process_started


_DIRECT_INVOCATION_TIMEOUT_SECONDS = 3 * 60 * 60
_DEFAULT_INVOCATION_TIMEOUT_SECONDS = {
    "development": 5 * 60 * 60,
    "review": 2 * 60 * 60,
    "publication": 60 * 60,
}
_MAX_FINAL_OUTPUT_BYTES = 1024 * 1024


class CodexCliBackend:
    """Runs untrusted role-scoped agents without Publisher GitHub credentials."""

    emits_execution_binding = True

    def __init__(
        self,
        executable: str = "codex",
        credential_provider: CredentialProvider | None = None,
        *,
        on_worker_started: Callable[[int], None] | None = None,
        on_worker_finished: Callable[[int], None] | None = None,
    ) -> None:
        self.executable = executable
        self.credential_provider = credential_provider
        self.on_worker_started = on_worker_started
        self.on_worker_finished = on_worker_finished

    def develop(
        self, request: dict[str, Any]
    ) -> DevelopmentResult | HumanBlockerResult:
        checkout = Path(_string(request, "checkout"))
        prompt = self._development_prompt(request)
        output, actual_thread = self._invoke_structured_output(
            request=request,
            prompt=prompt,
            checkout=checkout,
            thread_id=_optional_string(request, "thread_id"),
            repository=_optional_string(request, "repository"),
            schema=development_or_human_blocker_schema(),
            output_name="Development result",
            validate=parse_development_wire_result,
            initial_writable_checkout=True,
            default_deadline_seconds=_DEFAULT_INVOCATION_TIMEOUT_SECONDS[
                "development"
            ],
        )
        result = parse_development_wire_result(
            _json_object(output, "Development result")
        )
        if result["result_kind"] == "human_blocker":
            return HumanBlockerResult(
                thread_id=actual_thread,
                human_blockers=tuple(result["human_blockers"]),
            )
        return DevelopmentResult(
            thread_id=actual_thread,
            summary=str(result["summary"]),
        )

    def publication_schema_handshake(self, checkout: Path) -> tuple[str, str]:
        """Exercise the production Publication schema boundary once, read-only."""

        return self._invoke(
            prompt=(
                "这是一次受控 Structured Outputs schema handshake。不要读取或修改仓库，不要调用"
                "工具。仅返回 result_kind 为 human_blocker，三个 publication 字段为 null，"
                "human_blockers 为只含一条非空中文字符串的数组。"
            ),
            checkout=checkout,
            thread_id=None,
            schema=publication_or_human_blocker_schema(),
            writable_checkout=False,
        )

    @staticmethod
    def _development_prompt(request: dict[str, Any]) -> str:
        is_run_repair = request.get("acceptance_scope") == "run"
        is_parent_only = request.get("acceptance_scope") == "parent_only"
        repair_source = request.get("repair_source")
        repair_evidence = ""
        if repair_source is None:
            mode = (
                "Development Brief：以当前 Parent Issue 和代码事实为依据，以最小、完整、"
                "可维护的改动满足全部 Acceptance Criteria。"
                if is_parent_only
                else "Development Brief：以当前 Ticket 和代码事实为依据，以最小、完整、"
                "可维护的改动满足全部 Acceptance Criteria。"
            )
            heading = "Development Brief"
            prompt_input = _pretty(_development_context(request))
        elif repair_source == "git_integrity":
            evidence = request.get("git_integrity_evidence")
            if not isinstance(evidence, dict):
                raise ValueError("Git Integrity Repair requires git_integrity_evidence")
            subject = (
                "当前 Delivery Run"
                if is_run_repair
                else "当前 Parent Issue"
                if is_parent_only
                else "当前 Ticket"
            )
            mode = f"Git Integrity Repair：{subject}。"
            heading = "Git Integrity Repair Input"
            context = _development_context(request)
            repair_evidence = (
                f"Git Integrity Evidence (verbatim JSON):\n{_pretty(evidence)}"
            )
            prompt_input = (
                repair_evidence + f"\n\nDevelopment Brief:\n{_pretty(context)}"
            )
        elif repair_source == "acceptance":
            artifact = request.get("acceptance_artifact")
            if not isinstance(artifact, dict):
                raise ValueError(
                    "Acceptance Repair requires acceptance_artifact"
                )
            subject = (
                "当前 Delivery Run"
                if is_run_repair
                else "当前 Parent Issue"
                if is_parent_only
                else "当前 Ticket"
            )
            mode = f"Acceptance Repair：{subject}。"
            heading = "Acceptance Repair Input"
            context = _development_context(request)
            repair_evidence = (
                f"Acceptance Artifact (verbatim JSON):\n{_pretty(artifact)}"
            )
            prompt_input = (
                repair_evidence + f"\n\nDevelopment Brief:\n{_pretty(context)}"
            )
        elif repair_source == "required_checks":
            evidence = request.get("ci_evidence")
            if not isinstance(evidence, dict):
                raise ValueError("Required-Checks Repair requires ci_evidence")
            subject = (
                "当前 Delivery Run"
                if is_run_repair
                else "当前 Parent Issue"
                if is_parent_only
                else "当前 Ticket"
            )
            mode = f"Required-Checks Repair：{subject}。"
            heading = "Required-Checks Repair Input"
            context = _development_context(request)
            repair_evidence = f"CI Evidence (verbatim JSON):\n{_pretty(evidence)}"
            prompt_input = (
                repair_evidence + f"\n\nDevelopment Brief:\n{_pretty(context)}"
            )
        elif repair_source == "human_revision":
            feedback = request.get("human_feedback")
            if not isinstance(feedback, str) or not feedback.strip():
                raise ValueError("Human Revision requires human_feedback")
            mode = "Human Revision。"
            heading = "Human Revision Input"
            context = _development_context(request)
            repair_evidence = f"Maintainer Feedback (verbatim):\n{feedback}"
            prompt_input = (
                repair_evidence + f"\n\nDevelopment Brief:\n{_pretty(context)}"
            )
        elif repair_source == "merge_conflict":
            evidence = request.get("merge_conflict_evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError("Merge Conflict Repair requires merge_conflict_evidence")
            mode = "Merge Conflict Repair。"
            heading = "Merge Conflict Repair Input"
            context = _development_context(request)
            repair_evidence = f"Merge Conflict Evidence (verbatim):\n{evidence}"
            prompt_input = (
                repair_evidence + f"\n\nDevelopment Brief:\n{_pretty(context)}"
            )
        else:
            raise ValueError(f"unknown repair_source: {repair_source}")

        role = _development_role(request)
        if _uses_short_role_prompt(request):
            return _development_continuation_prompt(
                request,
                role=role,
                repair_source=repair_source,
                repair_evidence=repair_evidence,
            )
        if _uses_compact_repair_prompt(request, repair_source=repair_source):
            return _development_repair_prompt(
                request,
                role=role,
                mode=mode,
                repair_source=repair_source,
                repair_evidence=repair_evidence,
            )
        return (
            f"你是{role}。使用 skill:implement 完成开发或修复。"
            f"{mode}\n\n"
            + _development_contract(
                _development_context(request),
                acceptance_scope=request.get("acceptance_scope"),
                repair_scope=request.get("repair_scope"),
                repair_source=repair_source,
            )
            + _review_budget_block(request, reviewer=False)
            + "\n\n最后只输出完整 Development "
            'wire JSON：正常完成时 `{"result_kind":"development","summary":"...",'
            '"human_blockers":null}`；Human Blocker 时 summary 必须是 null。\n\n'
            f"{heading}:\n{prompt_input}"
        )

    def publication(
        self, request: dict[str, Any]
    ) -> PublicationResult | HumanBlockerResult:
        checkout = Path(_string(request, "checkout"))
        supplied_thread = request.get("thread_id")
        thread_id = (
            _string(request, "thread_id")
            if isinstance(supplied_thread, str)
            else None
        )
        prompt = self._publication_prompt(request)
        output, resumed_thread = self._invoke_publication(
            request=request,
            prompt=prompt,
            checkout=checkout,
            thread_id=thread_id,
            repository=_optional_string(request, "repository"),
        )
        artifact = _json_object(output, "Publication Artifact")
        blockers = parse_human_blockers(artifact)
        if blockers is not None:
            return HumanBlockerResult(
                thread_id=resumed_thread,
                human_blockers=blockers,
            )
        return PublicationResult(
            thread_id=resumed_thread,
            artifact=artifact,
        )

    def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
        """Create final-PR prose with a read-only release-narrative writer."""
        checkout = Path(_string(request, "checkout"))
        prompt = self._run_publication_prompt(request)
        output, thread_id = self._invoke_publication(
            request=request,
            prompt=prompt,
            checkout=checkout,
            thread_id=_optional_string(request, "thread_id"),
            repository=_optional_string(request, "repository"),
            artifact_validator=lambda artifact: PublicationArtifact.parse(
                artifact, delivery_run="final-run"
            ),
        )
        artifact = _json_object(output, "Run Publication Artifact")
        artifact["_thread_id"] = thread_id
        return artifact

    def _invoke_publication(
        self,
        *,
        request: dict[str, Any],
        prompt: str,
        checkout: Path,
        thread_id: str | None,
        repository: str | None,
        artifact_validator: Callable[[dict[str, Any]], object] | None = None,
    ) -> tuple[str, str]:
        def validate_publication(artifact: object) -> object:
            normalized = parse_publication_wire_result(artifact)
            if (
                normalized["result_kind"] == "publication"
                and artifact_validator is not None
            ):
                artifact_validator(normalized)
            return normalized

        return self._invoke_structured_output(
            request=request,
            prompt=prompt,
            checkout=checkout,
            thread_id=thread_id,
            repository=repository,
            schema=publication_or_human_blocker_schema(),
            output_name="Publication Artifact",
            validate=validate_publication,
            initial_writable_checkout=False,
            default_deadline_seconds=_DEFAULT_INVOCATION_TIMEOUT_SECONDS[
                "publication"
            ],
        )

    @staticmethod
    def _run_publication_prompt(request: dict[str, Any]) -> str:
        context = _prompt_context(
            request,
            "parent_issue_url",
            "prior_human_blockers",
            "human_response_history",
        )
        artifact = request.get("acceptance_artifact")
        if not isinstance(artifact, dict):
            raise ValueError("Final Run Publication requires acceptance_artifact")
        role = "本次 Final Run 的发布叙事工程师"
        if _uses_short_role_prompt(request):
            return _publication_continuation_prompt(request, role=role)
        return (
            f"你是{role}。阅读当前 checkout 的实际累计 diff，"
            "生成最终 Run PR 的语义标题和正文。\n\n"
            + _publication_contract(context, acceptance_scope="run")
            + "\n\nRun Acceptance Artifact (verbatim JSON):\n"
            + _pretty(artifact)
            + "\n\nFinal Run Publication Context:\n"
            + _pretty(context)
        )

    @staticmethod
    def _publication_prompt(request: dict[str, Any]) -> str:
        context = _prompt_context(
            request,
            "parent_issue_url",
            "task_issue_url",
            "prior_human_blockers",
            "human_response_history",
        )
        fallback_context = request.get("fallback_publication_context")
        artifact = request.get("acceptance_artifact")
        if isinstance(fallback_context, dict):
            artifact_input = (
                "Fallback Publication Context:\n"
                + _pretty(fallback_context)
                + "\n\n这是从已验证发布凭据投影的最小叙事事实。"
                "它不证明三个验收 lane 通过，不得把它写成 Acceptance Record、Review pass 或 CI pass；"
                "Publication 只负责生成叙事，不重新验收代码。"
            )
        elif isinstance(artifact, dict):
            artifact_input = "Acceptance Artifact (verbatim JSON):\n" + _pretty(artifact)
        else:
            raise ValueError(
                "Publication requires acceptance_artifact or fallback_publication_context"
            )
        role = _publication_role(request)
        if _uses_short_role_prompt(request):
            return _publication_continuation_prompt(request, role=role)
        evidence_phrase = (
            "下方最小 Fallback Publication Context"
            if isinstance(fallback_context, dict)
            else "下方完整独立验收证据"
        )
        return (
            f"你是{role}。依据当前 checkout 的实际 diff 和{evidence_phrase}，"
            "输出小型 Publication Artifact。\n\n"
            + _publication_contract(
                context,
                acceptance_scope=request.get("acceptance_scope"),
                fallback=isinstance(fallback_context, dict),
            )
            + "\n\n"
            + artifact_input
            + "\n\nPublication Context:\n"
            + _pretty(context)
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        checkout = Path(_string(request, "checkout"))
        prompt = self._review_prompt(request)
        output, thread_id = self._invoke_structured_output(
            request=request,
            prompt=prompt,
            checkout=checkout,
            thread_id=_optional_string(request, "thread_id"),
            repository=_optional_string(request, "repository"),
            schema=acceptance_schema(),
            output_name="Acceptance Artifact",
            validate=lambda value: AcceptanceArtifact.parse(value),
            initial_writable_checkout=False,
            default_deadline_seconds=_DEFAULT_INVOCATION_TIMEOUT_SECONDS["review"],
        )
        return ReviewResult(
            thread_id=thread_id,
            artifact=_json_object(output, "Acceptance Artifact"),
        )

    @staticmethod
    def _validate_acceptance_output(output: str) -> None:
        AcceptanceArtifact.parse(_json_object(output, "Acceptance Artifact"))

    def _invoke_structured_output(
        self,
        *,
        request: dict[str, Any],
        prompt: str,
        checkout: Path,
        thread_id: str | None,
        repository: str | None = None,
        schema: dict[str, Any],
        output_name: str,
        validate: Callable[[object], object],
        initial_writable_checkout: bool,
        default_deadline_seconds: float = _DIRECT_INVOCATION_TIMEOUT_SECONDS,
    ) -> tuple[str, str]:
        """Run one Invocation with at most two same-Thread output repairs."""

        event = request.get("_invocation_event")
        event_deadline_seconds = getattr(event, "deadline_seconds", None)
        deadline_seconds = _invocation_timeout_seconds(
            request,
            default=default_deadline_seconds,
            override=event_deadline_seconds,
        )
        deadline_started = time.monotonic()
        deadline_at = deadline_started + deadline_seconds
        notify = event if callable(event) else lambda _kind, **_facts: None
        notify(
            "started",
            requested_thread_id=thread_id,
            attempt_count=0,
            invocation_mode=request.get("_invocation_mode"),
        )
        current_thread = thread_id
        recovery_state = getattr(event, "recovery_state", {})
        attempt = int(recovery_state.get("output_attempt", 1))
        if attempt not in (1, 2, 3):
            raise CodexProcessError("Invalid persisted output step", recoverable=False)
        validation_error = str(recovery_state.get("validation_error", ""))[:2000]
        ordinary_recovery_used = bool(recovery_state.get("ordinary_recovery_used", False))
        capacity_recovery_count = int(recovery_state.get("capacity_recovery_count", 0))
        recovery_allowed = getattr(event, "recovery_allowed", None)
        currentness = request.get("_currentness_check")
        printed_steps: set[int] = set()

        def remember_thread(value: str) -> None:
            nonlocal current_thread
            if current_thread is not None and current_thread != value:
                raise CodexProcessError(
                    "Codex resume reported a different Thread ID", recoverable=False
                )
            notify("thread_started", reported_thread_id=value, attempt_count=attempt)
            current_thread = value

        while attempt <= 3:
            if callable(currentness) and not currentness():
                stale = CodexProcessError(
                    f"{output_name} currentness changed before Invocation"
                )
                notify("failed", attempt_count=attempt - 1, error=str(stale))
                raise stale
            notify("output_step", output_attempt=attempt, validation_error=validation_error)
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                deadline_error = CodexProcessError(
                    f"{output_name} exceeded its Invocation Deadline"
                )
                notify(
                    "failed",
                    attempt_count=attempt - 1,
                    error=str(deadline_error),
                    return_code=None,
                    signal=None,
                )
                raise deadline_error
            attempt_prompt = prompt
            if attempt > 1:
                attempt_prompt = _structured_output_repair_prompt(
                    output_name, validation_error[:2000]
                )
            try:
                execution_binding = request.get("_execution_binding")
                model = None
                reasoning_effort = None
                if isinstance(execution_binding, dict):
                    model = _optional_string(execution_binding, "model")
                    reasoning_effort = _optional_string(
                        execution_binding, "reasoning_effort"
                    )
                if attempt not in printed_steps:
                    self._print_execution_binding(
                        request,
                        thread_id=current_thread,
                        model=model,
                        reasoning_effort=reasoning_effort,
                    )
                    printed_steps.add(attempt)
                output, reported_thread = self._invoke(
                    prompt=attempt_prompt,
                    checkout=checkout,
                    thread_id=current_thread,
                    repository=repository,
                    schema=schema,
                    writable_checkout=initial_writable_checkout and attempt == 1,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    timeout=remaining,
                    deadline_at_monotonic=deadline_at,
                    on_thread=remember_thread,
                    validate_final_output=lambda value: validate(
                        _json_object(value, output_name)
                    ),
                )
            except InitialCredentialUnavailable:
                raise
            except BaseException as error:
                process_started = (
                    not isinstance(error, _CodexInvocationDeadlineError)
                    or error.process_started
                )
                if isinstance(error, _CodexInvocationDeadlineError):
                    error = CodexProcessError(
                        f"{output_name} exceeded its Invocation Deadline",
                        return_code=error.return_code,
                        signal_number=error.signal_number,
                        recoverable=process_started,
                    )
                resumable = (
                    isinstance(error, CodexProcessError)
                    and error.recoverable
                    and current_thread is not None
                    and callable(recovery_allowed)
                    and callable(currentness)
                    and currentness()
                    and recovery_allowed()
                )
                capacity = isinstance(error, CodexProcessError) and error.capacity_failure
                if resumable and (capacity or not ordinary_recovery_used):
                    assert callable(recovery_allowed) and callable(currentness)
                    if capacity:
                        capacity_recovery_count += 1
                    else:
                        ordinary_recovery_used = True
                    facts = {
                        "recovery_kind": "capacity" if capacity else "ordinary",
                        "ordinary_recovery_used": ordinary_recovery_used,
                        "capacity_recovery_count": capacity_recovery_count,
                        "error": _bounded_error(str(error)),
                        "machine_error": getattr(error, "machine_error", None),
                        "return_code": getattr(error, "return_code", None),
                        "signal": getattr(error, "signal_number", None),
                    }
                    notify("recovery_waiting", **facts)
                    if capacity:
                        for _ in range(30):
                            if not recovery_allowed():
                                raise CodexProcessError(
                                    "Execution recovery authorization changed",
                                    recoverable=False,
                                )
                            time.sleep(1)
                    if not recovery_allowed() or not currentness():
                        raise CodexProcessError(
                            "Execution recovery authorization changed", recoverable=False
                        )
                    notify("recovery_started", **facts)
                    deadline_at = time.monotonic() + deadline_seconds
                    continue
                failure_facts: dict[str, object] = {
                    "attempt_count": attempt if process_started else attempt - 1,
                    "error": _bounded_error(str(error)),
                    "return_code": getattr(error, "return_code", None),
                    "signal": getattr(error, "signal_number", None),
                }
                if process_started:
                    failure_facts["execution_interrupted"] = not getattr(error, "completed_output", False)
                    failure_facts["machine_error"] = getattr(error, "machine_error", None)
                notify("failed", **failure_facts)
                raise error
            if current_thread is not None and reported_thread != current_thread:
                mismatch = CodexProcessError(
                    "Codex resume reported a different Thread ID", recoverable=False
                )
                notify("failed", attempt_count=attempt, error=str(mismatch))
                raise mismatch
            current_thread = reported_thread
            try:
                validate(_json_object(output, output_name))
            except (CodexProcessError, ValueError) as error:
                validation_error = str(error)[:2000]
                if attempt < 3:
                    attempt += 1
                    continue
                notify(
                    "failed", attempt_count=attempt, error=validation_error,
                    execution_interrupted=False,
                )
                raise CodexProcessError(validation_error) from error
            if callable(currentness) and not currentness():
                stale = CodexProcessError(
                    f"{output_name} currentness changed before result application"
                )
                notify("failed", attempt_count=attempt, error=str(stale))
                raise stale
            notify(
                "completed",
                reported_thread_id=current_thread,
                attempt_count=attempt,
            )
            return output, current_thread
        raise AssertionError("unreachable")

    @staticmethod
    def _print_execution_binding(
        request: dict[str, Any],
        *,
        thread_id: str | None,
        model: str | None,
        reasoning_effort: str | None,
    ) -> None:
        binding = request.get("_execution_binding")
        if not isinstance(binding, dict):
            return
        role = request.get("_execution_role") or binding.get("role")
        revision = binding.get("profile_revision")
        emit_execution_binding(
            role=role,
            thread_id=thread_id,
            model=model or binding.get("model"),
            reasoning_effort=reasoning_effort or binding.get("reasoning_effort"),
            profile_revision=revision,
        )

    @staticmethod
    def _review_prompt(request: dict[str, Any]) -> str:
        context = _prompt_context(
            request,
            "parent_issue_url",
            "task_issue_url",
            "prior_human_blockers",
            "human_response_history",
            "fallback_ticket_records",
            "ticket_integration_records",
        )
        run_repair = (
            request.get("acceptance_scope") == "run"
            and request.get("repair_scope") == "run_repair"
        )
        candidate_run_acceptance = (
            request.get("acceptance_scope") == "run"
            and request.get("candidate_acceptance") is True
            and not run_repair
        )
        role = _review_role(request)
        if _uses_short_role_prompt(request, reviewer=True):
            return _review_continuation_prompt(request, role=role)
        if run_repair:
            candidate_instruction = (
                "这是 Run Repair Candidate Acceptance。当前 Validation Checkout 是将本轮 "
                "Run Repair Candidate 应用到准确 Run/default base 后的无提交合并预览；"
                "HEAD 保持 base 是正常现象。验收 Repair 进入完整 Run 后的结果，不能只审查局部 "
                "Repair diff。\n\n"
            )
        elif candidate_run_acceptance:
            candidate_instruction = (
                "这是 Candidate Run Acceptance。当前 Validation Checkout 是将本轮 "
                "Repair Candidate 应用到当前 default head 后的预期合并结果，HEAD 保持 "
                "default head 是正常现象。仍须按完整 Run Review Boundary 验收，"
                "不得把局部 Repair Candidate 的 diff 通过当作完整 Run 通过。\n\n"
            )
        else:
            candidate_instruction = ""
        current_review = _review_identity_block(request)
        previous_review = ""
        previous_artifact = request.get("previous_acceptance_artifact")
        if isinstance(previous_artifact, dict):
            previous_review = _previous_review_block(request, previous_artifact)
        else:
            previous_review = (
                "\n\n没有 Previous Acceptance Context 时，对完整 Review Boundary 建立基线；"
                "使用当前代码与需求独立形成本轮事实。"
            )
        prompt = (
            f"你是{role}。\n\n"
            + candidate_instruction
            + _acceptance_contract(
                context,
                acceptance_scope=request.get("acceptance_scope"),
                repair_scope=request.get("repair_scope"),
            )
            + _review_budget_block(request, reviewer=True)
            + current_review
            + previous_review
            + "\n\n"
            + "汇总三条 lane 的实际证据后，只输出符合 schema 的 Acceptance Artifact。"
            "每条 Finding 都必须写在最合适 lane 的 findings 中，并严格采用现有 schema 要求的"
            "字符串格式“问题：…；证据：…；必须修复：…；复验：…”，"
            "同一问题不得跨 lane 重复。"
            "任何当前范围内、有证据且必须修复的 Finding 都使该 lane 为 fail；有 Finding 时绝不能"
            "写 pass。pass 与 blocked 的 findings 必须为空；blocked 的 evidence 必须说明发生了什么、"
            "已经尝试什么、以及人必须做什么。没有 fail 但存在 blocked 才是 Human Blocker；"
            "只有三个 lane 都 pass 才接受。纯主观偏好或当前范围外的未来想法不构成 Finding。"
            "Reviewer 应一次报告当前审查中已经可证明的全部必须修复 Finding，但不得为追求穷尽而扩大"
            "Review Boundary。pass evidence 必须严格使用以下可复核标记：E2E 使用“操作或命令：…；"
            "退出码：…；结果：…”，Standards 使用“审查范围或基线：…；结论：…”，Spec 使用“已核对"
            "的验收标准：…；覆盖结论：…”。\n\n"
            f"Acceptance Context:\n{_pretty(context)}"
        )
        return prompt

    def _invoke(
        self,
        *,
        prompt: str,
        checkout: Path,
        thread_id: str | None,
        repository: str | None = None,
        schema: dict[str, Any] | None = None,
        writable_checkout: bool = True,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = _DIRECT_INVOCATION_TIMEOUT_SECONDS,
        deadline_at_monotonic: float | None = None,
        on_thread: Callable[[str], None] | None = None,
        validate_final_output: Callable[[str], object] | None = None,
    ) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="agent-run-codex-") as temp_name:
            temporary = Path(temp_name)
            output_path = temporary / "last-message.txt"
            schema_path = temporary / "schema.json"
            observed_thread = thread_id

            def report_thread(value: str) -> None:
                nonlocal observed_thread
                if on_thread is not None:
                    on_thread(value)
                observed_thread = value

            def accepted_output() -> tuple[str, str] | None:
                if validate_final_output is None or observed_thread is None:
                    return None
                try:
                    with output_path.open("rb") as output_file:
                        candidate = output_file.read(_MAX_FINAL_OUTPUT_BYTES + 1)
                    if len(candidate) > _MAX_FINAL_OUTPUT_BYTES:
                        return None
                    candidate_text = candidate.decode("utf-8")
                    validate_final_output(candidate_text)
                except (OSError, UnicodeError, ValueError, CodexProcessError):
                    return None
                return candidate_text, observed_thread

            if schema is not None:
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
            if thread_id is None:
                codex_arguments = [
                    self.executable,
                    "exec",
                    "--json",
                    "--color",
                    "never",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--cd",
                    str(checkout),
                ]
            else:
                codex_arguments = [
                    self.executable,
                    "exec",
                    "resume",
                    "--json",
                    "--dangerously-bypass-approvals-and-sandbox",
                    thread_id,
                ]
            if model is not None:
                codex_arguments.extend(["--model", model])
            if reasoning_effort is not None:
                codex_arguments.extend(
                    ["--config", f'model_reasoning_effort="{reasoning_effort}"']
                )
            if schema is not None:
                codex_arguments.extend(["--output-schema", str(schema_path)])
            codex_arguments.extend(
                ["--output-last-message", str(output_path), "-"]
            )
            try:
                adapter_directory = temporary / "gh-adapter"
                environment = worker_credential_environment(
                    temporary / "gh", adapter_directory
                )
                gh_targets = _worker_gh_targets(environment, cwd=checkout)
                real_gh_path = _controller_gh_target(
                    gh_targets, checkout=checkout, temporary=temporary
                )
                if real_gh_path is None:
                    raise WorkerSandboxError("gh is required for Worker GitHub reads")
                real_gh = str(real_gh_path)
                profile = (
                    load_github_app_profile()
                    if self.credential_provider is None
                    else None
                )
                if self.credential_provider is None:
                    repository = repository or _repository_hint_from_origin(checkout)
                else:
                    repository = None
                if self.credential_provider is None and repository is None:
                    raise WorkerSandboxError(
                        "当前 GitHub repository identity 不可确定，已拒绝 Worker GitHub read"
                    )
                hidden_paths = _worker_hidden_paths(profile, checkout=checkout)
                if repository is not None:
                    environment["GH_REPO"] = repository
                credential_exhausted = threading.Event()
                credential_failure: list[str] = []
                def report_credential_exhausted(message: str) -> None:
                    credential_failure.append(message)
                    credential_exhausted.set()

                if self.credential_provider is not None:
                    channel: HostGitHubReadChannel | WorkerCredentialChannel = WorkerCredentialChannel(
                        self.credential_provider,
                        gh_executable=real_gh,
                        gh_environment=environment,
                        repository=repository,
                        on_exhausted=report_credential_exhausted,
                    )
                elif profile is None:
                    channel = HostGitHubReadChannel(
                        gh_executable=real_gh,
                        gh_environment=dict(os.environ),
                        repository=repository,
                    )
                else:
                    channel = WorkerCredentialChannel(
                        _GitHubAppCredentialProvider(profile),
                        gh_executable=real_gh,
                        gh_environment=environment,
                        repository=repository,
                        on_exhausted=report_credential_exhausted,
                    )
                with channel as credentials:
                    try:
                        credentials.start(temporary / "credential.sock")
                    except WorkerCredentialError as error:
                        if _is_retryable_initial_credential_error(error):
                            raise InitialCredentialUnavailable(
                                "credential_unavailable",
                                http_status=error.http_status,
                            ) from error
                        raise
                    create_gh_access_adapter(
                        adapter_directory, temporary / "credential.sock", environment
                    )
                    arguments = bubblewrap_command(
                        codex_arguments,
                        checkout=checkout,
                        temporary=temporary,
                        writable_checkout=writable_checkout,
                        environment=environment,
                        hidden_paths=hidden_paths,
                        gh_adapter=adapter_directory / "gh",
                        gh_targets=gh_targets,
                    )
                    worker_timeout = timeout
                    if deadline_at_monotonic is not None:
                        worker_timeout = deadline_at_monotonic - time.monotonic()
                        if worker_timeout <= 0:
                            raise _CodexInvocationDeadlineError(
                                "Codex worker timed out", process_started=False
                            )
                    worker_options: dict[str, Any] = {
                        "cwd": checkout,
                        "prompt": prompt,
                        "environment": environment,
                        "timeout": worker_timeout,
                    }
                    if deadline_at_monotonic is not None:
                        worker_options["deadline_at_monotonic"] = (
                            deadline_at_monotonic
                        )
                    if self.credential_provider is not None or profile is not None:
                        worker_options.update(
                            {
                                "abort_event": credential_exhausted,
                                "abort_reason": lambda: credential_failure[0],
                            }
                        )
                    if on_thread is not None and "on_stdout_line" in inspect.signature(
                        run_worker_process
                    ).parameters:
                        worker_options["on_stdout_line"] = _thread_line_callback(
                            expected=thread_id, callback=report_thread
                        )
                    worker_pid: int | None = None

                    def worker_started(pid: int) -> None:
                        nonlocal worker_pid
                        worker_pid = pid
                        if self.on_worker_started is not None:
                            self.on_worker_started(pid)

                    if "on_process_started" in inspect.signature(
                        run_worker_process
                    ).parameters:
                        worker_options["on_process_started"] = worker_started
                    try:
                        result = run_worker_process(arguments, **worker_options)
                    finally:
                        if worker_pid is not None and self.on_worker_finished is not None:
                            self.on_worker_finished(worker_pid)
            except InitialCredentialUnavailable:
                raise
            except WorkerDeadlineExceeded as error:
                completed = accepted_output()
                if completed is not None:
                    return completed
                raise _CodexInvocationDeadlineError(
                    str(error), process_started=True
                ) from error
            except WorkerSandboxError as error:
                raise CodexProcessError(str(error), recoverable=False) from error
            except WorkerCredentialError as error:
                raise CodexProcessError(str(error), recoverable=False) from error
            result_thread = _thread_id(result.stdout)
            if result_thread is not None:
                if thread_id is not None and thread_id != result_thread:
                    raise _CodexThreadResumeError(
                        "Codex resume reported a different Thread ID", recoverable=False
                    )
                report_thread(result_thread)
            if result.returncode != 0:
                completed = accepted_output()
                if completed is not None:
                    return completed
            if result.returncode != 0:
                message = _terminal_error(result.stdout, result.stderr)
                binding_failed = _looks_like_worker_gh_binding_failure(
                    result.stdout,
                    result.stderr,
                    gh_targets=gh_targets,
                    gh_adapter=adapter_directory / "gh",
                )
                if binding_failed:
                    message = (
                        "worker_gh_binding_failed: Codex worker did not start"
                    )
                signal_number = -result.returncode if result.returncode < 0 else None
                capacity_failure, machine_error = _failure_diagnostic(result.stdout)
                if thread_id is not None:
                    raise _CodexThreadResumeError(
                        message,
                        return_code=result.returncode,
                        signal_number=signal_number,
                        recoverable=not binding_failed,
                        capacity_failure=capacity_failure,
                        machine_error=machine_error,
                    )
                raise CodexProcessError(
                    message,
                    return_code=result.returncode,
                    signal_number=signal_number,
                    recoverable=not binding_failed,
                    capacity_failure=capacity_failure,
                    machine_error=machine_error,
                )
            try:
                with output_path.open("rb") as output_file:
                    final_output = output_file.read(_MAX_FINAL_OUTPUT_BYTES + 1)
            except FileNotFoundError as error:
                raise CodexProcessError(
                    "Codex worker did not produce a final response"
                ) from error
            if len(final_output) > _MAX_FINAL_OUTPUT_BYTES:
                raise CodexProcessError(
                    "Codex worker final response is too large",
                    recoverable=False, completed_output=True,
                )
            reported_thread = _thread_id(result.stdout)
            if (
                thread_id is not None
                and reported_thread is not None
                and reported_thread != thread_id
            ):
                raise _CodexThreadResumeError(
                    "Codex resume reported a different Thread ID", recoverable=False
                )
            actual_thread = reported_thread or thread_id
            if actual_thread is None:
                raise CodexProcessError("Codex worker did not report a Thread ID")
            try:
                decoded = final_output.decode("utf-8")
            except UnicodeDecodeError as error:
                raise CodexProcessError(
                    "Codex worker final response is not valid UTF-8",
                    recoverable=False, completed_output=True,
                ) from error
            return decoded, actual_thread


def _looks_like_worker_gh_binding_failure(
    stdout: str,
    stderr: str,
    *,
    gh_targets: tuple[Path, ...],
    gh_adapter: Path,
) -> bool:
    """Classify bubblewrap mount failures before the Codex payload starts."""

    if stdout.strip():
        return False
    message = stderr.lstrip().casefold()
    if not message.startswith("bwrap:"):
        return False
    if not any(
        marker in message
        for marker in (
            "can't bind",
            "cannot bind",
            "bind mount",
            "can't open source",
            "cannot open source",
            "can't create file at",
            "cannot create file at",
            "can't mkdir parents for",
            "cannot mkdir parents for",
        )
    ):
        return False
    return any(
        str(path).casefold() in message
        for path in (*gh_targets, gh_adapter)
    )


def _controller_gh_target(
    targets: tuple[Path, ...], *, checkout: Path, temporary: Path
) -> Path | None:
    """Select a host-owned target, never a Worker-writable path."""

    worker_writable_roots = (checkout.resolve(), temporary.resolve())
    for target in targets:
        if not any(target.is_relative_to(root) for root in worker_writable_roots):
            return target
    return None


def _thread_id(output: str) -> str | None:
    for line in output.splitlines():
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("type") != "thread.started":
            continue
        found = value.get("thread_id")
        if isinstance(found, str) and found.strip():
            return found
    return None


def _is_retryable_initial_credential_error(error: WorkerCredentialError) -> bool:
    """Keep invalid credential configuration fail-closed before Worker launch.

    The Supervisor may retry unavailable token minting, but retrying a
    malformed token or a permissions/configuration rejection would turn a
    deterministic execution error into an endless wait.
    """

    permanent_markers = (
        "empty token",
        "invalid credential",
        "exact permissions",
        "github app id, installation id, and private key are required",
        "token response is invalid",
        "token response has no",
        "token response has an invalid expiry",
        "token response has an expired token",
        "could not sign github app jwt",
        "openssl is required",
    )
    message = str(error).lower()
    return not any(marker in message for marker in permanent_markers)


def _failure_diagnostic(stdout: str) -> tuple[bool, str | None]:
    """Machine failure types take precedence over compatibility message matching."""
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("type") not in {
            "turn.failed", "task_complete"
        }:
            continue
        error = value.get("error")
        if isinstance(error, dict) and "codex_error_info" in error:
            machine = error["codex_error_info"]
            diagnostic = machine if isinstance(machine, str) else json.dumps(machine, ensure_ascii=False)
            return machine == "server_overloaded", _bounded_error(diagnostic)[:2000]
        message = error.get("message") if isinstance(error, dict) else error
        return message == "Selected model is at capacity. Please try a different model.", None
    return False, None


def _terminal_error(stdout: str, stderr: str) -> str:
    candidates: dict[str, str] = {}
    for line in stdout.splitlines():
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        event_type = value.get("type")
        if event_type == "task_complete":
            nested = value.get("error")
            if isinstance(nested, dict) and isinstance(nested.get("message"), str):
                candidates["task_complete"] = nested["message"]
        elif event_type == "turn.failed":
            nested = value.get("error")
            if isinstance(nested, dict) and isinstance(nested.get("message"), str):
                candidates["turn.failed"] = nested["message"]
            elif isinstance(nested, str):
                candidates["turn.failed"] = nested
        if event_type not in {"task_complete", "turn.failed"}:
            nested = value.get("error")
            if isinstance(nested, dict) and isinstance(nested.get("message"), str):
                candidates.setdefault("error", nested["message"])
            elif isinstance(nested, str):
                candidates.setdefault("error", nested)
    raw = (
        candidates.get("task_complete")
        or candidates.get("error")
        or candidates.get("turn.failed")
        or stderr.strip()
        or "Codex worker failed"
    )
    return _bounded_error(_project_api_error(raw) or raw)


_API_ERROR_FIELDS = ("type", "code", "status", "message", "param")


def _project_api_error(value: str) -> str | None:
    decoder = json.JSONDecoder()
    for offset, character in enumerate(value):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(value, offset)
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict):
            continue
        nested = candidate.get("error")
        primary = nested if isinstance(nested, dict) else candidate
        projected: dict[str, object] = {}
        for field in _API_ERROR_FIELDS:
            sources = (primary, candidate) if primary is not candidate else (primary,)
            for source in sources:
                if field in source and _is_json_scalar(source[field]):
                    projected[field] = source[field]
                    break
        if projected:
            return json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    return None


def _is_json_scalar(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _bounded_error(value: str) -> str:
    return bounded_error(value)


def _invocation_timeout_seconds(
    request: dict[str, Any], *, default: float, override: object | None = None
) -> float:
    value = (
        request.get("_invocation_deadline_seconds", default)
        if override is None
        else override
    )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invocation Deadline must be a positive duration")
    try:
        timeout = float(value)
    except OverflowError as error:
        raise ValueError("Invocation Deadline must be a positive duration") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Invocation Deadline must be a positive duration")
    return timeout


def _thread_line_callback(
    *, expected: str | None, callback: Callable[[str], None] | None
) -> Callable[[str], None]:
    reported: str | None = None

    def consume(line: str) -> None:
        nonlocal reported
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(value, dict) or value.get("type") != "thread.started":
            return
        candidate = value.get("thread_id")
        if not isinstance(candidate, str) or not candidate.strip():
            raise CodexProcessError(
                "Codex worker reported an invalid Thread ID", recoverable=False
            )
        if reported is not None and candidate != reported:
            raise CodexProcessError(
                "Codex worker reported multiple Thread IDs", recoverable=False
            )
        if expected is not None and candidate != expected:
            raise CodexProcessError(
                "Codex resume reported a different Thread ID", recoverable=False
            )
        reported = candidate
        if callback is not None:
            callback(candidate)

    return consume


def _json_object(value: str, name: str) -> dict[str, Any]:
    try:
        loaded: object = json.loads(value)
    except json.JSONDecodeError as error:
        raise CodexProcessError(f"{name} is invalid JSON: {error}") from error
    if not isinstance(loaded, dict):
        raise CodexProcessError(f"{name} must be an object")
    return loaded


def _human_blocker_output(value: str, name: str) -> tuple[str, ...] | None:
    try:
        loaded: object = json.loads(value)
    except json.JSONDecodeError as error:
        if value.lstrip().startswith(("{", "[")):
            raise CodexProcessError(
                f"{name} looks like invalid structured JSON: {error}"
            ) from error
        return None
    try:
        blockers = parse_human_blockers(loaded)
    except ValueError as error:
        raise CodexProcessError(f"{name} Human Blocker is invalid: {error}") from error
    if blockers is None and isinstance(loaded, (dict, list)):
        raise CodexProcessError(
            f"{name} structured result does not match the Human Blocker contract"
        )
    return blockers


def _prompt_context(request: dict[str, Any], *fields: str) -> dict[str, Any]:
    context = {
        field: request[field]
        for field in fields
        if field != "human_response_history"
        and field in request
        and request[field] is not None
    }
    if "human_response_history" in fields:
        latest_response = _latest_maintainer_response(request)
        if latest_response is not None:
            context["latest_maintainer_response"] = latest_response
    return context


def _latest_maintainer_response(request: dict[str, Any]) -> str | None:
    history = request.get("human_response_history")
    if not isinstance(history, list):
        return None
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        response = item.get("response")
        if isinstance(response, str) and response.strip():
            return response
    return None


def _development_role(request: dict[str, Any]) -> str:
    if request.get("acceptance_scope") == "run":
        return "本次 Delivery Run 的修复工程师"
    if request.get("acceptance_scope") == "parent_only":
        return "当前 Parent Issue 的开发工程师"
    return "当前 Ticket 的开发工程师"


def _review_role(request: dict[str, Any]) -> str:
    run_repair = (
        request.get("acceptance_scope") == "run"
        and request.get("repair_scope") == "run_repair"
    )
    if run_repair:
        return "独立 Run Repair 验收工程师"
    if (
        request.get("acceptance_scope") == "run"
        and request.get("candidate_acceptance") is True
    ):
        return "独立 Candidate Run Acceptance 验收工程师"
    if request.get("acceptance_scope") == "run":
        return "独立 Run 整体验收工程师"
    if request.get("acceptance_scope") == "parent_only":
        return "当前 Parent-only Candidate 的独立验收工程师"
    return "当前 Ticket Candidate 的独立集成验收工程师"


def _publication_role(request: dict[str, Any]) -> str:
    return {
        "ticket": "当前 Ticket PR 的发布叙事工程师",
        "parent_only": "当前 Parent-only PR 的发布叙事工程师",
        "run": "当前 Run Repair PR 的发布叙事工程师",
    }[_scope_kind(request.get("acceptance_scope"))]


def _uses_short_role_prompt(
    request: dict[str, Any], *, reviewer: bool = False
) -> bool:
    mode = request.get("_invocation_mode")
    if mode == "new-thread":
        return False
    thread_id = request.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        return False
    if reviewer and not isinstance(request.get("current_review_identity"), dict):
        return False
    if mode == "resume" or reviewer:
        return True
    blockers = request.get("prior_human_blockers")
    return isinstance(blockers, list) and bool(blockers)


def _uses_compact_repair_prompt(
    request: dict[str, Any], *, repair_source: object
) -> bool:
    if repair_source is None or request.get("_invocation_mode") == "new-thread":
        return False
    thread_id = request.get("thread_id")
    return isinstance(thread_id, str) and bool(thread_id.strip())


def _human_continuation_block(request: dict[str, Any]) -> str:
    evidence: dict[str, Any] = {}
    blockers = request.get("prior_human_blockers")
    if isinstance(blockers, list) and blockers:
        evidence["current_human_blockers"] = blockers
    latest_response = _latest_maintainer_response(request)
    if latest_response is not None:
        evidence["latest_maintainer_response"] = latest_response
    if not evidence:
        return ""
    return "\n\n当前仍需处理的动态证据（verbatim）：\n" + _pretty(evidence)


def _current_object_block(
    request: dict[str, Any], *, publication: bool = False
) -> str:
    facts = _prompt_context(request, "parent_issue_url", "task_issue_url")
    if publication:
        if isinstance(request.get("fallback_publication_context"), dict):
            facts["current_publication_evidence"] = "最小 Fallback Publication Context"
        elif isinstance(request.get("acceptance_artifact"), dict):
            facts["current_publication_evidence"] = "完整独立验收证据"
    if not facts:
        return ""
    return "\n\n当前对象事实（verbatim）：\n" + _pretty(facts)


def _development_continuation_prompt(
    request: dict[str, Any],
    *,
    role: str,
    repair_source: object,
    repair_evidence: str,
) -> str:
    if repair_source is None:
        instruction = (
            "继续完成你负责的当前开发交付。检查当前 checkout 中已有进展，以真实代码、当前需求和"
            "已执行验证为准，完成剩余实现、风险相称的验证与自行检查。"
        )
    else:
        instruction = (
            "继续完成你负责的当前修复交付。检查当前 checkout 中已有修复进展，围绕下面仍然有效的"
            "原始证据完成剩余修复、直接回归处理和风险相称的验证。"
            + ("\n\n当前 Repair Evidence：\n" + repair_evidence)
        )
    return (
        f"你是{role}。{instruction}"
        + _current_object_block(request)
        + _review_budget_block(request, reviewer=False)
        + _human_continuation_block(request)
        + "\n\n完成后只输出 Development wire JSON；summary 只陈述实际改动、实际验证和已知限制。"
    )


def _development_repair_prompt(
    request: dict[str, Any],
    *,
    role: str,
    mode: str,
    repair_source: object,
    repair_evidence: str,
) -> str:
    return (
        f"你是{role}。使用 skill:implement 完成当前定向修复。{mode}\n\n"
        + _review_boundary_instruction(
            request.get("acceptance_scope"),
            repair_scope=request.get("repair_scope"),
        )
        + "\n"
        + _repair_contract(repair_source)
        + "\n"
        + _repair_completion_instruction(repair_source)
        + "\n\n当前 Issue URL 用于确认修复对象和需求边界。以原始 Repair Evidence、当前 "
        "checkout 和已经掌握的需求为主要输入；只有在无法判断修复范围、证据与"
        "需求冲突，或需要核对具体 Acceptance Criteria 时，再通过只读 `gh issue view` "
        "回查对应 Issue。"
        + _current_object_block(request)
        + "\n\n当前 Repair Evidence：\n"
        + repair_evidence
        + "\n\n采用最小且可维护的修复完成上述要求；范围外能力、可选重构和未来扩展"
        "不属于本轮交付。"
        "当前 checkout 最终保留的交付修改会整体成为新的 Candidate Commit；只整理工作树，"
        "不暂存、commit、改写 Git 历史或写入远端。"
        + "\n\n完成修复后，自行检查当前工作树并完成与风险相称的验证。本轮只负责修复，"
        "不形成独立验收或确定性门禁结论。本轮不需要启动开发侧 Reviewer。"
        + "\n\n保留本轮需要交付的代码、测试、文档和配置，清理本轮产生的临时、构建和测试产物。"
        + "\n\n"
        + _human_blocker_instruction()
        + _review_budget_block(request, reviewer=False)
        + "\n\n完成后只输出完整 Development wire JSON；summary 只陈述实际改动、实际验证和已知限制。"
    )


def _review_continuation_prompt(request: dict[str, Any], *, role: str) -> str:
    return (
        f"你是{role}。继续完成你负责的当前独立验收。以当前 Validation Checkout 和下面的准确"
        "验收对象为准，完成尚未收口的核验，并只输出当前对象的新 Acceptance Artifact。"
        + _review_identity_block(request)
        + _review_budget_block(request, reviewer=True)
        + _human_continuation_block(request)
    )


def _review_budget_block(request: dict[str, Any], *, reviewer: bool) -> str:
    context = request.get("review_budget_context")
    if context is None:
        return ""
    if not isinstance(context, dict):
        raise ValueError("review_budget_context must be an object")
    remaining = context.get("remaining_review_attempts")
    if type(remaining) is not int or remaining < 0:
        raise ValueError("remaining_review_attempts must be a non-negative integer")
    if reviewer:
        current = context.get("current_review_attempt")
        if type(current) is not int or current < 1:
            raise ValueError("current_review_attempt must be a positive integer")
        summary = (
            f"这是当前对象的第 {current} 次独立验收；根据当前可用额度，"
            f"本轮结束后最多还可自动启动 {remaining} 次独立验收。"
        )
    else:
        completed = context.get("completed_review_attempts")
        if type(completed) is not int or completed < 0:
            raise ValueError("completed_review_attempts must be a non-negative integer")
        summary = (
            f"当前对象已经完成 {completed} 次独立验收；根据当前可用额度，"
            f"最多还可自动启动 {remaining} 次独立验收。"
        )
    return (
        "\n\n"
        + summary
        + "该信息只用于合理组织本轮工作并尽量一次收口，不改变验收标准；"
        "不得隐瞒、降级或放行必须修复的问题。"
    )


def _publication_continuation_prompt(
    request: dict[str, Any], *, role: str
) -> str:
    return (
        f"你是{role}。继续完成你负责的当前发布叙事。以当前 checkout 和当前发布对象中仍然有效的"
        "事实为准，生成准确、简洁的 commit message、PR title 与 PR body，并只输出 Publication "
        "wire JSON。"
        + _current_object_block(request, publication=True)
        + _human_continuation_block(request)
    )


def _structured_output_repair_prompt(output_name: str, contract_error: str) -> str:
    if output_name == "Development result":
        return (
            "你已完成当前开发或修复工作。本轮唯一任务是根据已完成的真实工作，重新输出满足 "
            "contract 的 Development wire JSON。\n\n"
            f"Contract error：{contract_error}\n\n"
            "只输出修正后的 JSON；不重新执行开发、验证或工具调用。"
        )
    if output_name == "Acceptance Artifact":
        return (
            "你已完成当前独立验收。本轮唯一任务是根据已完成的审查事实，重新输出满足 contract "
            "的 Acceptance Artifact。\n\n"
            f"Contract error：{contract_error}\n\n"
            "只输出修正后的 JSON；不重新执行审查、验证或工具调用。"
        )
    if output_name == "Publication Artifact":
        return (
            "你已完成当前发布叙事。本轮唯一任务是根据已经形成的发布事实，重新输出满足 contract 的 "
            "Publication wire JSON。\n\n"
            f"Contract error：{contract_error}\n\n"
            "只输出修正后的 JSON；不重新读取项目、改写交付事实或调用工具。"
        )
    raise ValueError(f"unknown structured output role: {output_name}")


def _review_identity_block(request: dict[str, Any]) -> str:
    identity = request.get("current_review_identity")
    if not isinstance(identity, dict):
        return ""
    scope = _scope_kind(request.get("acceptance_scope"))
    labels: tuple[tuple[str, str], ...]
    if scope == "run" and request.get("repair_scope") == "run_repair":
        labels = (
            ("当前 Run base", "run_base_sha"),
            ("当前 Repair Candidate", "repair_candidate_sha"),
            ("当前 expected merge tree", "expected_merge_tree"),
        )
    elif scope == "run":
        labels = (
            ("当前 default base", "default_base_sha"),
            ("当前 Run head", "run_head_sha"),
            ("当前 expected merge tree", "expected_merge_tree"),
        )
    else:
        labels = (
            ("当前 reviewed base", "reviewed_base_sha"),
            ("当前 Candidate", "reviewed_candidate_sha"),
            ("当前 Candidate tree", "reviewed_candidate_tree"),
        )
    lines = ["\n\nCurrent Review Boundary Identity:"]
    for label, key in labels:
        if key in identity:
            lines.append(f"- {label}：{identity[key]}")
    return "\n".join(lines)


def _previous_review_block(
    request: dict[str, Any], artifact: dict[str, Any]
) -> str:
    scope = _scope_kind(request.get("acceptance_scope"))
    identity = request.get("previous_review_identity", {})
    labels: tuple[tuple[str, str], ...]
    previous_is_run_repair = isinstance(identity, dict) and (
        "run_base_sha" in identity or "repair_candidate_sha" in identity
    )
    previous_is_run = isinstance(identity, dict) and (
        "default_base_sha" in identity or "run_head_sha" in identity
    )
    if scope == "run" and previous_is_run_repair:
        title = "Previous Run Repair Acceptance Context"
        labels = (
            ("Previous Run base", "run_base_sha"),
            ("Previous Repair Candidate", "repair_candidate_sha"),
            ("Previous expected merge tree", "expected_merge_tree"),
        )
        guidance = (
            "优先核销上一轮 Findings，审查上一验收对象到当前 Repair Candidate 的完整 repair "
            "delta 与直接回归，尤其检查它对完整 Run 合并预览产生的变化。缺少具体风险依据时，"
            "避免对未变化代码重复完整扫描；当前证据或实际影响需要时，自主扩大检查范围。"
        )
        closing = "上一轮 Artifact 不能授权当前合并预览。"
    elif scope == "run" and previous_is_run:
        title = "Previous Run Acceptance Context"
        labels = (
            ("Previous default base", "default_base_sha"),
            ("Previous Run head", "run_head_sha"),
            ("Previous expected merge tree", "expected_merge_tree"),
        )
        guidance = (
            "优先核销上一轮 Findings，审查上一验收对象到当前最终合并预览的完整 repair delta "
            "与直接回归，尤其检查当前 Run Repair 产生的变化。缺少具体风险依据时，避免对未变化"
            "代码重复完整扫描；当前证据或实际影响需要时，自主扩大检查范围。"
        )
        closing = "上一 Artifact 只描述上一组 default base、Run head 和预期合并结果，不能授权当前最终合并预览。"
    else:
        title = "Previous Acceptance Context"
        labels = (
            ("Previous reviewed base", "reviewed_base_sha"),
            ("Previous reviewed Candidate", "reviewed_candidate_sha"),
            ("Previous Candidate tree", "reviewed_candidate_tree"),
        )
        guidance = (
            "优先核销上一轮 Findings，审查上一 Candidate 到当前 Candidate 的完整 repair delta "
            "与直接回归，尤其检查与原 Findings 相关的变化。缺少具体风险依据时，避免对未变化代码"
            "重复完整扫描；当前证据或实际影响需要时，自主扩大检查范围。"
        )
        closing = "上一轮 Artifact 只描述上一 Candidate，不能授权当前 Candidate。"
    identity_lines = [f"\n\n## {title}", "", "下面是紧邻上一轮 Reviewer 的完整 Acceptance Artifact。"]
    if isinstance(identity, dict):
        for label, key in labels:
            if key in identity:
                identity_lines.append(f"{label}：{identity[key]}")
    identity_lines.extend(
        [
            "",
            "Previous Acceptance Artifact（verbatim JSON）:",
            _pretty(artifact),
            "",
            guidance,
            f"{closing}本轮只输出当前对象的新 Acceptance Artifact，不输出 Finding closure 表或逐项对照。",
        ]
    )
    return "\n".join(identity_lines)


def _development_context(request: dict[str, Any]) -> dict[str, Any]:
    return _prompt_context(
        request,
        "parent_issue_url",
        "task_issue_url",
        "prior_human_blockers",
        "human_response_history",
    )


def _scope_kind(acceptance_scope: object) -> str:
    if acceptance_scope == "parent_only":
        return "parent_only"
    if acceptance_scope == "run":
        return "run"
    return "ticket"


def _issue_context_instruction(
    context: dict[str, Any],
    *,
    acceptance_scope: object = "ticket",
    directed_repair: bool = False,
) -> str:
    scope = _scope_kind(acceptance_scope)
    if scope == "parent_only":
        scope_instruction = "parent_issue_url 是当前 Parent-only Delivery 的完整需求源。"
        if not directed_repair:
            scope_instruction = (
                "parent_issue_url 是当前 Parent-only Delivery 的完整需求源，"
                "开始前必须通过只读 `gh issue view` 读取其 title、body 和 "
                "Acceptance Criteria。"
            )
    elif scope == "run":
        scope_instruction = (
            "parent_issue_url 是当前 Delivery Run 的完整 Parent 需求源和 "
            "Acceptance Criteria。"
        )
        if not directed_repair:
            scope_instruction = (
                "parent_issue_url 是当前 Delivery Run 的完整 Parent 需求源和 Acceptance "
                "Criteria，开始前必须通过只读 `gh issue view` 读取它，并按当前 "
                "Run 的范围独立读取最终 Ticket Set、依赖和必要的整体约束。"
            )
    else:
        scope_instruction = (
            "parent_issue_url 只提供整体背景、术语和当前 Ticket 明确引用且完成其 "
            "Acceptance Criteria 所需的约束；它本身不增加当前 Ticket 的工作项。"
        )
        if not directed_repair:
            scope_instruction = (
                "parent_issue_url 只提供整体背景、术语和当前 Ticket 明确引用且完成其 "
                "Acceptance Criteria 所需的约束，开始前必须通过只读 `gh issue view` "
                "读取它；它本身不增加当前 Ticket 的工作项。"
            )
    task_instruction = (
        " task_issue_url 是当前 Ticket 的唯一立即交付合同；其 title、body 和 "
        "Acceptance Criteria 优先于 Parent 中可独立交付的 sibling 或 follow-on 能力。"
        if scope == "ticket" and "task_issue_url" in context
        else ""
    )
    if task_instruction and not directed_repair:
        task_instruction = (
            " task_issue_url 是当前 Ticket 的唯一立即交付合同，开始前也必须通过只读 "
            "`gh issue view` 读取它；其 title、body 和 Acceptance Criteria 优先于 Parent 中"
            "可独立交付的 sibling 或 follow-on 能力。"
        )
    repair_lookup_instruction = ""
    if directed_repair:
        repair_lookup_instruction = (
            " 当前 Issue URL 用于确认本轮修复对象和需求边界。以本轮原始 Repair "
            "Evidence、当前 checkout 和已经掌握的当前需求为主要输入；如果无法据此判断"
            "修复范围、证据与当前需求存在冲突，或需要核对具体 Acceptance Criteria，再通过"
            "只读 `gh issue view` 回查对应 Issue。不要仅因开始本轮修复而重复读取"
            "没有变化的需求。"
        )
    return (
        "动态 Context 中的 URL 不是需求摘要。"
        + scope_instruction
        + task_instruction
        + repair_lookup_instruction
        + " Issue 评论、历史 PR、旧 Artifact、开发者总结和上游 Agent 结论只能作为调查线索，"
        "不能覆盖当前需求或单独构成验收证据。"
        + _resume_recheck_instruction(context)
    )


def _review_boundary_instruction(
    acceptance_scope: object, *, repair_scope: object = None
) -> str:
    scope = _scope_kind(acceptance_scope)
    if scope == "parent_only":
        return (
            "当前 Review Boundary 是完整 Parent Issue；Parent 的完整 Acceptance Criteria 和"
            "Candidate 对其新增或改变路径直接造成的工程风险都属于本轮范围。"
        )
    if scope == "run":
        if repair_scope == "run_repair":
            return (
                "当前 Review Boundary 是 Run Repair 的完整 Parent、最终 Ticket Set、依赖关系、"
                "累计变更、跨 Ticket 交互和预期合并结果；当前 checkout 是 Run Branch 加入 repair "
                "Candidate 后的无提交合并预览，HEAD 保持 Run Branch base 是正常现象。不得把局部 "
                "Repair Candidate 单独通过当作整体验收通过。"
            )
        return (
            "当前 Review Boundary 是完整 Parent、最终 Ticket Set、依赖关系、累计变更、跨 Ticket "
            "交互和预期合并结果；不得把单 Ticket 的通过当作整体验收通过。"
        )
    return (
        "当前 Review Boundary 是 Ticket Contract：当前 Ticket 的 title、body 和 Acceptance "
        "Criteria，以及 Candidate 对新增或改变路径直接造成的工程风险。Parent Context 只用于"
        "解释背景和必要约束；sibling/follow-on Ticket 不会自动进入本轮范围。"
    )


def _repair_contract(repair_source: object) -> str:
    if repair_source is None:
        return ""
    if repair_source == "acceptance":
        return (
            "Acceptance Artifact 是未经改写的修复依据；其中当前 Review Boundary 内的 `findings` 是"
            "本轮必须处理的问题证据，但不限定实现方案，也不表示问题只存在于列出的示例。"
            "结合当前代码理解根因，检查同一决策点直接影响的场景，并覆盖本次修复可能造成的直接回归。"
            "`evidence` 中的 `Deferred to #N：…` 和 `Non-blocking observation：…` 不是自动修改"
            "指令。只有解决 Finding、防止本次修复直接回归或满足当前 Acceptance Criteria 确有需要时，"
            "才调整相关 evidence 或代码。"
        )
    if repair_source == "git_integrity":
        return (
            "Git Integrity Evidence 是未经改写的修复依据。恢复当前受管 checkout 的合法 Git 边界，"
            "只处理证据及其直接影响；不要执行 commit、reset、rebase、merge、push 或其他 Git 历史写入。"
            "当前职责只整理文件树为合法、完整、可交付的 Candidate，不自行创建 Candidate Commit。"
        )
    if repair_source == "required_checks":
        return (
            "CI Evidence 是未经改写的修复依据；只修复失败 Required Check 及其直接影响，不得绕过"
            "检查、删除测试、放宽断言或把其他建议自动扩成工作项。"
        )
    if repair_source == "human_revision":
        return (
            "维护者反馈是未经改写的修复依据；先用当前代码和事实核验其影响，只处理完成当前修复所需"
            "的最小范围。"
        )
    if repair_source == "merge_conflict":
        return (
            "合并冲突证据是未经改写的修复依据；只解决真实冲突及其直接影响，不借机扩大功能或绕过"
            "既有验收。"
        )
    return ""


def _repair_completion_instruction(repair_source: object) -> str:
    if repair_source != "acceptance":
        return ""
    return (
        "Acceptance Repair 的完成条件还包括：解决当前 Review Boundary 内全部有证据支持的"
        "Finding，并取得足以证明根因关闭、直接同族场景和直接回归受到覆盖的可复核证据。"
    )


def _development_closeout_instruction(repair_source: object) -> str:
    if repair_source is not None:
        return (
            "本轮以提供的原始 Repair Evidence 为权威修复入口。处理问题及避免直接回归所需的"
            "影响后，自行检查当前工作树并完成与风险相称的验证，然后返回 Development wire "
            "JSON。本轮只负责修复，不形成独立验收或确定性门禁结论。本轮不需要启动开发侧 Reviewer。"
        )
    return (
        "完成实现和受影响路径验证后，先自行检查当前完整工作树、已知风险与未处理问题。根据实际"
        "风险判断独立预检能否增加价值；低风险局部改动可以直接收口，需要独立预检时默认最多进行"
        "一个 Development Preflight Round。一轮可以包含多个不同风险方向的审查型 subagent，"
        "其数量、分工和检查命令由你决定。"
        "派发审查型 subagent 时使用 fork_turns: \"none\"，并由你提供完成审查所需的中立任务"
        "事实、当前范围和真实证据；让审查基于代码与需求独立建立判断，而不是继承你的开发结论、"
        "辩护或预设答案。其他探索、调研或并行实现 subagent 是否继承上下文，由你根据任务需要"
        "决定。内部审查遵循 skill:code-review 的 Standards/Spec 方法，并覆盖当前未提交工作树及"
        "未跟踪的交付内容。汇总本轮 findings，修复有证据支持的问题并自行重跑受影响验证；本轮"
        "内部预检至此结束，不常规启动第二轮内部 Reviewer。内部预检不形成 Acceptance Artifact，"
        "也不宣布独立验收通过。"
    )


def _development_contract(
    context: dict[str, Any],
    *,
    acceptance_scope: object,
    repair_source: object,
    repair_scope: object = None,
) -> str:
    return (
        _issue_context_instruction(
            context,
            acceptance_scope=acceptance_scope,
            directed_repair=repair_source is not None,
        )
        + "\n\n"
        + _review_boundary_instruction(acceptance_scope, repair_scope=repair_scope)
        + "\n"
        + _repair_contract(repair_source)
        + "\n"
        + _repair_completion_instruction(repair_source)
        + "\n\n"
        + "采用最小且可维护的方案完整满足当前范围，并遵循现有仓库约定。验收示例不是完整问题"
        "空间；涉及共享决策点时，检查当前需求直接影响的同族场景，避免只修补一个表面案例。"
        "范围外能力、可选重构和未来扩展不属于本轮交付。"
        + "\n\n"
        + "当前 checkout 最终保留的交付修改（包括应交付的未跟踪文件）会整体成为本轮 Candidate "
        "Commit 的内容。可以用只读 Git 命令理解历史；只整理当前工作树，不暂存、commit、改写 "
        "Git 历史或写入远端。修正先前改动时直接形成当前正确文件树。"
        + "\n\n"
        + _human_blocker_instruction()
        + "\n\n阅读适用的 AGENTS.md、相关实现、测试和真实调用入口；在适合的位置采用 TDD。"
        "根据实际风险取得最低充分证据：覆盖直接影响的成功路径、失败路径和边界情况，"
        "并优先从真实用户入口复验核心路径。选择相关单测、typecheck、lint、完整测试套件或其他"
        "检查时记录实际命令、exit code、可观察结果和必要状态变化；完整测试套件不是每轮默认的"
        "固定门槛，未运行的检查不得声称已通过。不要用 mock、单元测试或代码阅读替代能够真实"
        "运行的核心路径。"
        "局部编辑及 Repair 先围绕失败证据、根因和直接回归选择相关测试；共享状态、生命周期、"
        "持久化、公共接口、测试基础设施或依赖变化应覆盖直接调用方及同族场景。影响范围不明"
        "或具体 Finding 要求时，可以提前运行完整套件，并说明扩大范围的理由。"
        "稳定候选是已知实现修改、相关验证及需先处理的问题已经收口、准备交付独立验收的结果，"
        "每次编辑后的短暂停顿不算收口。完整套件失败后，先定向诊断、修复并验证受影响路径，"
        "再对修复后的稳定候选完整复验；不要每改一处就重跑全量。"
        "本项目中，skill:implement 的结束验证按上述责任执行：独立 Acceptance 的 E2E 负责"
        "稳定候选的完整验证，开发自测不能替代独立验收。"
        "验证记录应说明实际代码或工作树、测试范围、命令、结果和相关环境；代码、测试、依赖"
        "或相关环境变化后，重新判断旧结果的适用性，不得把旧候选的通过直接用于新候选。"
        + "\n\n"
        + "本角色合同定义当前调用的完成边界；skill:implement 中关于最终完整测试、开发侧 Review "
        "或提交代码的通用建议，不替代这里的风险相称验证、Initial Development 预检原则和只修改"
        "工作树的 Git 边界。"
        + "\n\n"
        + "完成条件是：当前 Review Boundary 的 Acceptance Criteria 已完整实现；直接影响的路径已有"
        "与风险相称的验证；没有已知 blocker；当前 checkout 中保留的全部未提交内容都适合作为"
        "本次交付。"
        + "\n\n"
        + _development_closeout_instruction(repair_source)
        + "\n\n在当前 checkout 中检查全部未提交内容：保留本任务需要交付的代码、测试、文档和配置，"
        "清理本次产生的临时、构建和测试产物。仅长期、可再生且不应版本控制的项目产物可以加入"
        "`.gitignore`；不得用 `.gitignore` 隐藏应交付内容。若在 checkout 外创建临时路径，必须使"
        "其可定位、只服务本次任务并在完成前清理，不得进行宽泛删除。\n\n"
        "交付前再次确认没有遗漏未提交内容、临时产物或外部写入。"
    )


def _acceptance_contract(
    context: dict[str, Any], *, acceptance_scope: object, repair_scope: object = None
) -> str:
    return (
        _issue_context_instruction(context, acceptance_scope=acceptance_scope)
        + "\n\n"
        + _review_boundary_instruction(acceptance_scope, repair_scope=repair_scope)
        + "\n\n不要依赖开发者总结、自测、开发侧 Review、PR 文案或 Publication Artifact；使用真实 "
        "Git/gh 自行建立事实。本次验收必须分别形成 E2E、Standards 和 Spec 三种独立视角，"
        "并对这三个维度分别形成可复核的证据与结论，完整写入 Acceptance Artifact。"
        "你对三个维度的最终判断负责。E2E 负责当前稳定 Candidate 或"
        "合并预览的完整测试与必要检查，按适用 AGENTS.md 和仓库测试指南执行，独立取得实际结果；"
        "Standards 与 Spec 默认使用静态证据和验证具体问题所需的最小命令，避免重复相同"
        "的完整测试套件，除非某个具体 Finding 确实需要。"
        "记录实际验证对象、命令、exit code、结果和相关环境；代码、测试、依赖或相关环境变化后，"
        "重新判断旧结果的适用性，不能把旧 Candidate 或其他合并预览的通过直接用于当前对象。"
        "完整测试失败时提供具体失败证据和复验要求，使修复先定向诊断与验证、收口后再完整复验；"
        "你仍保持只读，不负责修改候选。未执行或未完成的检查不得声称通过。"
        + "\n\n"
        + "每次 Reviewer 都必须调用 `skill:code-review` 作为审查方法。根据当前 Review Boundary 和"
        "实际风险组织审查、subagent 与验证，不要求每个维度对应一个独立 subagent。所有审查或"
        "评价型 subagent 必须使用 fork_turns: \"none\"，并只接收当前范围、对象身份和中立事实，"
        "不得继承开发者的修复叙事或结论。没有具体风险依据时，不重复同类审查或昂贵测试。"
        + "\n\n"
        + "只有同时满足以下条件的问题才进入 `findings`：属于当前 Review Boundary；有可复现、"
        "可定位的证据；违反明确当前需求或硬性工程合同，或者形成具体风险；保持现状会使当前"
        "验收对象不可接受；并且能由当前 Change Job 修复。明确需求或硬性合同的真实缺陷即使修复"
        "很小也仍是 Finding。同一根因的多个表现应合并报告，并说明受影响的直接同族场景。"
        "已由明确 sibling/follow-on Issue 承接的内容只以 `Deferred to #N：…` 写入最相关 lane 的"
        "`evidence`。不影响当前可接受性的主观偏好、可选重构或轻微维护性问题不得进入 findings；"
        "确有后续价值时可记为 `Non-blocking observation：…`，没有实际后续价值的轻微问题直接"
        "省略。两者都不得改变 lane 状态或成为自动修复指令。"
        + "\n\n"
        + "Validation Checkout 是只读的，不得创建、修改或删除其中的文件。可构建、测试和"
        "产生验证中间产物，但任何需要写入的内容必须放在 checkout 外可定位、只服务本轮的"
        "临时路径，并在结束前清理；不得修复源码、测试、配置或 `.gitignore`，也不得整理"
        "交付内容。不得 commit、push、merge、关闭或修改 GitHub。"
    )


def _publication_contract(
    context: dict[str, Any], *, acceptance_scope: object, fallback: bool = False
) -> str:
    evidence_contract = (
        "这是 Deterministic Ticket Fallback Publication。当前没有 Fresh Acceptance Artifact；"
        "Fallback Publication Receipt 不是验收结论。Evidence 使用 Fallback Publication Context 中"
        "实际提供的 Receipt 投影，准确区分最近一次独立 Reviewer 已审查的对象、其后产生的 Candidate"
        " delta 与 repair delta、Git Integrity 结果以及失败来源。不得声称当前 Candidate 通过任何验收 lane、"
        "关闭 Finding、Required Checks 通过或取得 Run Acceptance。"
        if fallback
        else (
            "PR 叙事必须有四个非空二级标题：What Problem This Solves 写改前限制、改后能力和覆盖"
            "边界；Why This Change Was Made 写关键设计路径与约束，不要逐文件罗列；User Impact 写"
            "用户可执行的结果和兼容或迁移行为；Evidence 只使用完整独立验收三条 lane 的实际证据，"
            "每条使用“场景 → 实际操作或命令 → 可观察结果”。不得用“tests passed”“已验证”“修复完成”"
            "等没有场景、操作和结果的空泛表述，不得把开发者自述当作验证事实。Evidence 中的"
            "`Deferred to #N：…` 和 `Non-blocking observation：…` 只是非阻塞审查信息，不得描述为"
            "当前交付范围的交付成果、已实现能力或 User Impact。CI、Candidate、SHA、门禁和生命周期"
            "事实不得写入叙事。"
        )
    )
    contract = (
        _issue_context_instruction(context, acceptance_scope=acceptance_scope)
        + "\n\n只读取事实：不得修改 checkout、执行 Git/GitHub 写操作、执行验收或替代人工批准。"
        "若在 checkout 外创建临时路径，必须使其可定位、只服务本次任务并在完成前清理。"
        + "\n\n"
        + "PR 叙事必须有四个非空二级标题：What Problem This Solves、Why This Change Was Made、"
        "User Impact 和 Evidence。"
        + evidence_contract
        + "\n\n不得包含 closing keywords。commit_message 与 pr_title 都必须各自采用 Conventional "
        "Commit 语义标题格式 `type: summary` 或 `type(scope): summary`，其中 type 只能是 "
        "feat、fix、improve、refactor、docs、test、chore；不要使用自然语言标题。"
        + "\n\n"
        + _publication_human_blocker_instruction()
    )
    return contract


def _resume_recheck_instruction(context: dict[str, Any]) -> str:
    if "prior_human_blockers" not in context:
        return ""
    return (
        "prior_human_blockers 是当前仍需重新核验的未经改写求助内容，"
        "latest_maintainer_response（如有）是维护者最新的未经改写回复；"
        "不表示问题已经解决；必须重新读取权威来源、重新检查受影响工作，然后继续或报告"
        "更新后的 Human Blocker。"
    )


def _human_blocker_instruction() -> str:
    return (
        "若出现确实必须由人处理的外部权限、GitHub 访问、产品决定、敏感凭证或不可替代"
        "外部操作，停止当前阶段且只输出完整 Development wire JSON "
        '`{"result_kind":"human_blocker","summary":null,'
        '"human_blockers":["发生了什么；尝试了什么；人必须做什么"]}`。'
        "不要把可自行修复的问题作为 Human Blocker。"
    )


def _publication_human_blocker_instruction() -> str:
    return (
        "若出现确实必须由人处理的外部权限、GitHub 访问、产品决定、敏感凭证或不可替代"
        "外部操作，停止当前阶段且只输出完整 Publication wire JSON "
        '`{"result_kind":"human_blocker","commit_message":null,'
        '"pr_title":null,"pr_body_markdown":null,'
        '"human_blockers":["发生了什么；尝试了什么；人必须做什么"]}`。'
        "不要把可自行修复的问题作为 Human Blocker。"
    )


def _worker_hidden_paths(
    profile: object, *, checkout: Path | None = None
) -> tuple[Path, ...]:
    paths: list[Path] = []
    for variable in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME"):
        value = os.environ.get(variable)
        if value:
            paths.append(Path(value))
    paths.append(GitHubAppProfileStore().path)
    private_key_path = getattr(profile, "private_key_path", None)
    if isinstance(private_key_path, Path):
        paths.append(private_key_path)
    data_home = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ).expanduser()
    paths.append(data_home / "agent-run")
    paths.append(RunLocatorIndex.default().path.parent)
    paths.append(ensure_executor_runtime_directory())
    runtime_home = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.geteuid()}")
    ).expanduser()
    paths.extend((runtime_home / "bus", runtime_home / "systemd" / "private"))
    if checkout is not None:
        resolved_checkout = checkout.resolve()
        state_roots: set[Path] = set()
        local_root = resolved_checkout / ".agent-run"
        if local_root.exists():
            state_roots.add(local_root)
        for parent in resolved_checkout.parents:
            if parent.name == "worktrees" and parent.parent.name == ".agent-run":
                state_roots.add(parent.parent)
        configured_root = os.environ.get("AGENT_RUN_INTERNAL_STATE_ROOT")
        if configured_root:
            state_roots.add(Path(configured_root).expanduser())
        for root in state_roots:
            paths.extend(
                root / child
                for child in ("runs", "task-control", "profiles", ".lock")
            )
    return tuple(paths)


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
