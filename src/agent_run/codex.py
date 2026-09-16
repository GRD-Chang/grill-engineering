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
from agent_run.development_prompts import development_prompt
from agent_run.prompt_context import structured_output_repair_prompt
from agent_run.publication_prompts import (
    publication_continuation_prompt,
    publication_prompt,
)
from agent_run.reviewer_prompts import review_continuation_prompt, review_prompt
from agent_run.github_auth import _GitHubAppCredentialProvider
from agent_run.github import _repository_hint_from_origin
from agent_run.github_auth_profile import (
    GitHubAppProfileStore,
    load_github_app_profile,
)
from agent_run.error_safety import bounded_error
from agent_run.execution_binding import emit_execution_binding
from agent_run.executor_environment import ensure_executor_runtime_directory
from agent_run.paths import app_data_root
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
        continuation_prompt = self._development_prompt(
            request, force_continuation=True
        )
        output, actual_thread = self._invoke_structured_output(
            request=request,
            prompt=prompt,
            continuation_prompt=continuation_prompt,
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
                "你是负责提交说明输出格式检查的工程师。本次不读取或修改仓库，不调用工具。"
                "仅返回 result_kind 为 human_blocker，commit_message、pr_title 和 "
                "pr_body_markdown 为 null，human_blockers 为只含一条非空中文字符串的数组。"
            ),
            checkout=checkout,
            thread_id=None,
            schema=publication_or_human_blocker_schema(),
            writable_checkout=False,
        )

    @staticmethod
    def _development_prompt(
        request: dict[str, Any], *, force_continuation: bool = False
    ) -> str:
        return development_prompt(request, force_continuation=force_continuation)

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
            continuation_prompt=publication_continuation_prompt(request),
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
            continuation_prompt=publication_continuation_prompt(request, final_run=True),
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
        continuation_prompt: str,
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
            continuation_prompt=continuation_prompt,
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
        return publication_prompt(request, final_run=True)

    @staticmethod
    def _publication_prompt(request: dict[str, Any]) -> str:
        return publication_prompt(request)

    def review(self, request: dict[str, Any]) -> ReviewResult:
        checkout = Path(_string(request, "checkout"))
        prompt = self._review_prompt(request)
        output, thread_id = self._invoke_structured_output(
            request=request,
            prompt=prompt,
            continuation_prompt=review_continuation_prompt(request),
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
        continuation_prompt: str | None = None,
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
        role_prompt = prompt

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
            attempt_prompt = role_prompt
            if validation_error:
                attempt_prompt = structured_output_repair_prompt(
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
                    writable_checkout=initial_writable_checkout and not validation_error,
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
                    if not validation_error and continuation_prompt is not None:
                        role_prompt = continuation_prompt
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
        return review_prompt(request)

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
    paths.append(app_data_root())
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
