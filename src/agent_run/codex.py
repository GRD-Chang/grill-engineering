from __future__ import annotations

import inspect
import json
import math
import re
import shutil
import sys
import threading
import tempfile
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
from agent_run.github_auth import mint_read_only_installation_credential
from agent_run.error_safety import bounded_error
from agent_run.worker_sandbox import (
    WorkerSandboxError,
    bubblewrap_command,
    create_gh_access_adapter,
    run_worker_process,
    worker_credential_environment,
)
from agent_run.worker_credentials import (
    CredentialProvider,
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
    ) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.signal_number = signal_number


class _CodexThreadResumeError(CodexProcessError):
    pass


class CodexCliBackend:
    """Runs untrusted role-scoped agents without Publisher GitHub credentials."""

    emits_execution_binding = True

    def __init__(
        self,
        executable: str = "codex",
        credential_provider: CredentialProvider = mint_read_only_installation_credential,
    ) -> None:
        self.executable = executable
        self.credential_provider = credential_provider

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
            schema=development_or_human_blocker_schema(),
            output_name="Development result",
            validate=parse_development_wire_result,
            initial_writable_checkout=True,
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
            prompt_input = (
                f"Acceptance Artifact (verbatim JSON):\n{_pretty(artifact)}"
                f"\n\nDevelopment Brief:\n{_pretty(context)}"
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
            prompt_input = (
                f"CI Evidence (verbatim JSON):\n{_pretty(evidence)}"
                f"\n\nDevelopment Brief:\n{_pretty(context)}"
            )
        elif repair_source == "human_revision":
            feedback = request.get("human_feedback")
            if not isinstance(feedback, str) or not feedback.strip():
                raise ValueError("Human Revision requires human_feedback")
            mode = "Human Revision。"
            heading = "Human Revision Input"
            context = _development_context(request)
            prompt_input = (
                f"Maintainer Feedback (verbatim):\n{feedback}"
                f"\n\nDevelopment Brief:\n{_pretty(context)}"
            )
        elif repair_source == "merge_conflict":
            evidence = request.get("merge_conflict_evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError("Merge Conflict Repair requires merge_conflict_evidence")
            mode = "Merge Conflict Repair。"
            heading = "Merge Conflict Repair Input"
            context = _development_context(request)
            prompt_input = (
                f"Merge Conflict Evidence (verbatim):\n{evidence}"
                f"\n\nDevelopment Brief:\n{_pretty(context)}"
            )
        else:
            raise ValueError(f"unknown repair_source: {repair_source}")

        role = (
            "本次 Delivery Run 的修复工程师"
            if is_run_repair
            else "当前 Parent Issue 的开发工程师"
            if is_parent_only
            else "当前 Ticket 的开发工程师"
        )
        return (
            f"你是负责{role}。使用 skill:implement 完成开发或修复。"
            f"{mode}\n\n"
            + _development_contract(
                _development_context(request),
                acceptance_scope=request.get("acceptance_scope"),
                repair_scope=request.get("repair_scope"),
                repair_source=repair_source,
            )
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
            schema=publication_or_human_blocker_schema(),
            output_name="Publication Artifact",
            validate=validate_publication,
            initial_writable_checkout=False,
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
        return (
            "你是本次 Final Run 的发布叙事工程师。阅读当前 checkout 的实际累计 diff，"
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
        artifact = request.get("acceptance_artifact")
        if not isinstance(artifact, dict):
            raise ValueError("Publication requires acceptance_artifact")
        artifact_input = "Acceptance Artifact (verbatim JSON):\n" + _pretty(artifact)
        if request.get("acceptance_scope") == "run":
            return (
                "你是本次 Run Repair 的发布叙事工程师。依据当前 checkout 的实际 diff 和"
                "下方完整独立验收证据，输出小型 Publication Artifact。\n\n"
                + _publication_contract(
                    context, acceptance_scope=request.get("acceptance_scope")
                )
                + "\n\n"
                + artifact_input
                + "\n\nPublication Context:\n"
                + _pretty(context)
            )
        return (
            "你是本次交付的发布叙事工程师。依据当前 checkout 的实际 diff 和下方完整独立"
            "验收证据，输出小型 Publication Artifact。\n\n"
            + _publication_contract(
                context, acceptance_scope=request.get("acceptance_scope")
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
            schema=acceptance_schema(),
            output_name="Acceptance Artifact",
            validate=lambda value: AcceptanceArtifact.parse(value),
            initial_writable_checkout=False,
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
        schema: dict[str, Any],
        output_name: str,
        validate: Callable[[object], object],
        initial_writable_checkout: bool,
    ) -> tuple[str, str]:
        """Run one Invocation with at most two same-Thread output repairs."""

        event = request.get("_invocation_event")
        notify = event if callable(event) else lambda _kind, **_facts: None
        notify(
            "started",
            requested_thread_id=thread_id,
            attempt_count=0,
            invocation_mode=request.get("_invocation_mode"),
        )
        current_thread = thread_id
        validation_error = ""
        currentness = request.get("_currentness_check")
        if request.get("_invocation_mode") == "resume" and thread_id is not None:
            prompt = (
                "这是一次因前次调用失败而继续的同 Thread Resume。请基于当前 workspace "
                "重新核验权威输入和实际工作，再满足完整阶段 contract。\n\n" + prompt
        )
        for attempt in range(1, 4):
            if callable(currentness) and not currentness():
                stale = CodexProcessError(
                    f"{output_name} currentness changed before Invocation"
                )
                notify("failed", attempt_count=attempt - 1, error=str(stale))
                raise stale
            attempt_prompt = prompt
            if attempt > 1:
                attempt_prompt = (
                    f"上一输出未通过本地 {output_name} contract。只重新输出完整 JSON，"
                    "不要修改文件或继续开发。校验错误：" + validation_error[:2000]
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
                self._print_execution_binding(
                    request,
                    thread_id=current_thread,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                output, reported_thread = self._invoke(
                    prompt=attempt_prompt,
                    checkout=checkout,
                    thread_id=current_thread,
                    schema=schema,
                    writable_checkout=initial_writable_checkout and attempt == 1,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    on_thread=lambda value: notify(
                        "thread_started",
                        reported_thread_id=value,
                        attempt_count=attempt,
                    ),
                )
            except InitialCredentialUnavailable:
                raise
            except BaseException as error:
                notify(
                    "failed",
                    attempt_count=attempt,
                    error=_bounded_error(str(error)),
                    return_code=getattr(error, "return_code", None),
                    signal=getattr(error, "signal_number", None),
                )
                raise
            if current_thread is not None and reported_thread != current_thread:
                mismatch = CodexProcessError("Codex resume reported a different Thread ID")
                notify("failed", attempt_count=attempt, error=str(mismatch))
                raise mismatch
            current_thread = reported_thread
            try:
                validate(_json_object(output, output_name))
            except (CodexProcessError, ValueError) as error:
                validation_error = str(error)
                if attempt < 3:
                    continue
                notify("failed", attempt_count=attempt, error=validation_error)
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
        print(
            "Agent Execution Binding: "
            f"role={role} "
            f"thread={'resume' if thread_id is not None else 'new'} "
            f"model={model or binding.get('model')} "
            f"reasoning_effort={reasoning_effort or binding.get('reasoning_effort')} "
            f"profile_revision={revision} "
            f"thread_id={thread_id if thread_id is not None else 'none'}",
            file=sys.stderr,
            flush=True,
        )

    @staticmethod
    def _review_prompt(request: dict[str, Any]) -> str:
        context = _prompt_context(
            request,
            "parent_issue_url",
            "task_issue_url",
            "prior_human_blockers",
            "human_response_history",
        )
        candidate_run_acceptance = (
            request.get("acceptance_scope") == "run"
            and request.get("candidate_acceptance") is True
        )
        role = (
            "独立 Candidate Run Acceptance 验收工程师"
            if candidate_run_acceptance
            else "独立 Run Repair 验收工程师"
            if request.get("acceptance_scope") == "run"
            and request.get("repair_scope") == "run_repair"
            else "独立 Run 整体验收工程师"
            if request.get("acceptance_scope") == "run"
            else "独立 Fresh Acceptance 验收工程师"
        )
        candidate_instruction = (
            "这是 Candidate Run Acceptance。当前 Validation Checkout 是将本轮 "
            "Repair Candidate 应用到当前 default head 后的预期合并结果，HEAD 保持 "
            "default head 是正常现象。仍须按完整 Run Review Boundary 验收，"
            "不得把局部 Repair Candidate 的 diff 通过当作完整 Run 通过。\n\n"
            if candidate_run_acceptance
            else ""
        )
        prompt = (
            f"你是{role}。\n\n"
            + candidate_instruction
            + _acceptance_contract(
                context,
                acceptance_scope=request.get("acceptance_scope"),
                repair_scope=request.get("repair_scope"),
            )
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
        schema: dict[str, Any] | None = None,
        writable_checkout: bool = True,
        model: str | None = None,
        reasoning_effort: str | None = None,
        on_thread: Callable[[str], None] | None = None,
    ) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="agent-run-codex-") as temp_name:
            temporary = Path(temp_name)
            output_path = temporary / "last-message.txt"
            schema_path = temporary / "schema.json"
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
                real_gh = shutil.which("gh", path=environment.get("PATH"))
                if real_gh is None:
                    raise WorkerSandboxError("gh is required for Worker GitHub reads")
                credential_exhausted = threading.Event()
                credential_failure: list[str] = []
                def report_credential_exhausted(message: str) -> None:
                    credential_failure.append(message)
                    credential_exhausted.set()
                with WorkerCredentialChannel(
                    self.credential_provider,
                    gh_executable=real_gh,
                    gh_environment=environment,
                    on_exhausted=report_credential_exhausted,
                ) as credentials:
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
                    )
                    worker_options: dict[str, Any] = {
                        "cwd": checkout,
                        "prompt": prompt,
                        "environment": environment,
                        "timeout": 3 * 60 * 60,
                        "abort_event": credential_exhausted,
                        "abort_reason": lambda: credential_failure[0],
                    }
                    if on_thread is not None and "on_stdout_line" in inspect.signature(
                        run_worker_process
                    ).parameters:
                        worker_options["on_stdout_line"] = _thread_line_callback(
                            expected=thread_id, callback=on_thread
                        )
                    result = run_worker_process(arguments, **worker_options)
            except InitialCredentialUnavailable:
                raise
            except (WorkerSandboxError, WorkerCredentialError) as error:
                raise CodexProcessError(str(error)) from error
            if result.returncode != 0:
                message = _terminal_error(result.stdout, result.stderr)
                signal_number = -result.returncode if result.returncode < 0 else None
                if thread_id is not None:
                    raise _CodexThreadResumeError(
                        message,
                        return_code=result.returncode,
                        signal_number=signal_number,
                    )
                raise CodexProcessError(
                    message,
                    return_code=result.returncode,
                    signal_number=signal_number,
                )
            if not output_path.exists():
                raise CodexProcessError("Codex worker did not produce a final response")
            reported_thread = _thread_id(result.stdout)
            if (
                thread_id is not None
                and reported_thread is not None
                and reported_thread != thread_id
            ):
                raise _CodexThreadResumeError(
                    "Codex resume reported a different Thread ID"
                )
            actual_thread = reported_thread or thread_id
            if actual_thread is None:
                raise CodexProcessError("Codex worker did not report a Thread ID")
            return output_path.read_text(encoding="utf-8"), actual_thread


def _thread_id(output: str) -> str | None:
    for line in output.splitlines():
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        found = _find_thread_id(value)
        if found is not None:
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
            raise CodexProcessError("Codex worker reported an invalid Thread ID")
        if reported is not None and candidate != reported:
            raise CodexProcessError("Codex worker reported multiple Thread IDs")
        if expected is not None and candidate != expected:
            raise CodexProcessError("Codex resume reported a different Thread ID")
        reported = candidate
        if callback is not None:
            callback(candidate)

    return consume


def _find_thread_id(value: object) -> str | None:
    if isinstance(value, dict):
        direct = value.get("thread_id")
        if isinstance(direct, str):
            return direct
        for child in value.values():
            found = _find_thread_id(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_thread_id(child)
            if found is not None:
                return found
    return None


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
    return {field: request[field] for field in fields if field in request and request[field] is not None}


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
    context: dict[str, Any], *, acceptance_scope: object = "ticket"
) -> str:
    scope = _scope_kind(acceptance_scope)
    if scope == "parent_only":
        scope_instruction = (
            "parent_issue_url 是当前 Parent-only Delivery 的完整需求源，开始前必须通过只读 "
            "`gh issue view` 读取其 title、body 和 Acceptance Criteria。"
        )
    elif scope == "run":
        scope_instruction = (
            "parent_issue_url 是当前 Delivery Run 的完整 Parent 需求源和 Acceptance Criteria，开始前必须通过只读 "
            "`gh issue view` 读取它，并按当前 Run 的范围独立读取最终 Ticket Set、依赖和必要的"
            "整体约束。"
        )
    else:
        scope_instruction = (
            "parent_issue_url 只提供整体背景、术语和当前 Ticket 明确引用且完成其 Acceptance "
            "Criteria 所需的约束，开始前必须通过只读 `gh issue view` 读取它；它本身不增加"
            "当前 Ticket 的工作项。"
        )
    task_instruction = (
        " task_issue_url 是当前 Ticket 的唯一立即交付合同，开始前也必须通过只读 `gh issue view` "
        "读取它；其 title、body 和 Acceptance Criteria 优先于 Parent 中可独立交付的 sibling "
        "或 follow-on 能力。"
        if scope == "ticket" and "task_issue_url" in context
        else ""
    )
    return (
        "动态 Context 中的 URL 不是需求摘要。"
        + scope_instruction
        + task_instruction
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
            "本轮必须处理的问题。"
            "`evidence` 中的 `Deferred to #N：…` 和 `Non-blocking observation：…` 不是自动修改"
            "指令。只有解决 Finding、防止本次修复直接回归或满足当前 Acceptance Criteria 确有需要时，"
            "才调整相关 evidence 或代码。"
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
        "Acceptance Repair 的完成条件还包括：逐项解决当前 Review Boundary 内的每个 Finding，"
        "按每条 Finding 自带的 `复验` 要求执行验证并取得充分、可复核的证据；不能以一次笼统的"
        "风险验证替代逐项复验。"
    )


def _development_contract(
    context: dict[str, Any],
    *,
    acceptance_scope: object,
    repair_source: object,
    repair_scope: object = None,
) -> str:
    return (
        _issue_context_instruction(context, acceptance_scope=acceptance_scope)
        + "\n\n"
        + _review_boundary_instruction(acceptance_scope, repair_scope=repair_scope)
        + "\n"
        + _repair_contract(repair_source)
        + "\n"
        + _repair_completion_instruction(repair_source)
        + "\n\n"
        + "目标是最小充分改动：完整满足当前范围的 Acceptance Criteria，处理本次改动直接造成的"
        "工程风险，同时不增加无关行为、状态、依赖、配置、公开入口或抽象层。优先沿用直接适用"
        "的现有 Module、Interface 和仓库约定；只有当前正确性、可测试性、已经存在的具体重复"
        "或既有设计确有需要时，才做局部重构。不要为未来需求、其他 Ticket、假想调用方或可能"
        "复用增加通用框架、配置、回调、状态、Adapter 或公开 Interface。代码稳定并确认每处改动"
        "服务当前范围后，删除不需要的代码、状态、分支、配置和依赖；达到完成条件后停止扩展。"
        + "\n\n"
        + "当前 checkout 是程序管理的受管开发工作区。程序会用新的 Candidate Commit 记录每次 "
        "Development 或 Repair 的结果，Git 历史只向前推进。你可以使用 `git log`、`git show`、"
        "`git diff` 等只读操作检查历史和旧版本，但只修改当前 checkout 的文件树。如果先前 "
        "Candidate 中有文件改错，直接在当前 checkout 删除、恢复或重写相关内容，并将修正保留为"
        "未提交变更；不要回退、替换或修改旧 commit。不得执行暂存、commit、`commit --amend`、"
        "`reset`、`rebase`、`revert`、`cherry-pick`、切换到旧 commit 或其他 branch、merge、push，"
        "以及其他会移动、创建或改写 Git 历史的操作。Agent 返回后，程序会通过 Controller/Publisher "
        "根据当前 checkout 中保留的完整结果创建新的不可变 Candidate Commit，并执行后续 Git/GitHub "
        "交付；你只整理 checkout，不执行这些写入。因此，新的 Candidate 可以撤销、删除或重写先前 "
        "Candidate 引入的内容，最终 diff 可以比上一轮更小。"
        + "\n\n"
        + _human_blocker_instruction()
        + "\n\n阅读适用的 AGENTS.md、相关实现、测试和真实调用入口；在适合的位置采用 TDD。"
        "根据实际改动风险自主选择最低充分验证：覆盖直接影响的成功路径、失败路径和边界情况，"
        "并优先从真实用户入口复验核心路径。选择相关单测、typecheck、lint、完整测试套件或其他"
        "检查时记录实际命令、exit code、可观察结果和必要状态变化；完整测试套件不是每轮默认的"
        "固定门槛，未运行的检查不得声称已通过。不要用 mock、单元测试或代码阅读替代能够真实"
        "运行的核心路径。"
        + "\n\n"
        + "完成条件是：当前 Review Boundary 的 Acceptance Criteria 已完整实现；直接影响的路径已有"
        "与风险相称的验证；没有已知 blocker；当前 checkout 中保留的全部未提交内容都适合作为"
        "本次交付。低风险局部改动可以自行做简短收口检查；大型、跨模块或触及认证、权限、持久化、"
        "并发、数据完整性、外部副作用或公开契约的改动，"
        "应使用 `skill:code-review` 或定向 Reviewer 取得足够审查。根据实际改动和新发现的风险自主"
        "选择审查方式与复查强度；没有具体风险依据时，避免重复或嵌套相同的 Review。发现 blocking "
        "finding 后修复对应问题，重跑受影响验证并取得有效复查。"
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
        "并将每条 lane 的证据和结论完整写入 Acceptance Artifact。E2E 默认负责代码稳定后的广泛"
        "运行验证；Standards 与 Spec 默认使用静态证据和验证具体问题所需的最小命令，避免重复相同"
        "的完整测试套件，除非某个具体 Finding 确实需要。"
        + "\n\n"
        + "`skill:code-review` 是 Standards 与 Spec 可使用的推荐审查 SOP。根据当前 Review Boundary "
        "和实际风险选择审查分工与复核强度，确保 E2E、Standards 和 Spec 三种独立视角均形成可复核"
        "结论。没有具体风险依据时，避免重复派发同类 Reviewer、嵌套相同 Review，或由多个视角重复"
        "执行相同的昂贵测试。不得用父 Reviewer 自己的判断替代缺失的独立审查视角；派发或验证遇到"
        "问题时，先处理具体问题再形成可复核结论。"
        + "\n\n"
        + "Reviewer 应一次报告当前 Review Boundary 内已经能够证明的全部必须修复 Finding，但不得为追求穷尽"
        "而扩大 Review Boundary 或进行无边界探索。`findings` 只包含当前 Change Job 必须处理、"
        "有可复核证据且能由当前 Job 修复的问题；已由明确 sibling/follow-on Issue 承接的内容只以"
        "`Deferred to #N：…` 写入最相关 lane 的 `evidence`，纯维护性建议、可选重构和文件大小偏好"
        "只以 `Non-blocking observation：…` 写入 `evidence`。两者都不得进入 `findings`、改变 lane"
        "状态或成为自动修复指令。"
        + "\n\n"
        + "Validation Checkout 是只读的，不得创建、修改或删除其中的文件。可构建、测试和"
        "产生验证中间产物，但任何需要写入的内容必须放在 checkout 外可定位、只服务本轮的"
        "临时路径，并在结束前清理；不得修复源码、测试、配置或 `.gitignore`，也不得整理"
        "交付内容。不得 commit、push、merge、关闭或修改 GitHub。"
    )


def _publication_contract(
    context: dict[str, Any], *, acceptance_scope: object
) -> str:
    return (
        _issue_context_instruction(context, acceptance_scope=acceptance_scope)
        + "\n\n只读取事实：不得修改 checkout、执行 Git/GitHub 写操作、执行验收或替代人工批准。"
        "若在 checkout 外创建临时路径，必须使其可定位、只服务本次任务并在完成前清理。"
        + "\n\n"
        + "PR 叙事必须有四个非空二级标题：What Problem This Solves 写改前限制、改后能力和覆盖"
        "边界；Why This Change Was Made 写关键设计路径与约束，不要逐文件罗列；User Impact 写"
        "用户可执行的结果和兼容或迁移行为；Evidence 只使用完整独立验收三条 lane 的实际证据，"
        "每条使用“场景 → 实际操作或命令 → 可观察结果”。不得用“tests passed”“已验证”“修复完成”"
        "等没有场景、操作和结果的空泛表述，不得把开发者自述当作验证事实。Evidence 中的"
        "`Deferred to #N：…` 和 `Non-blocking observation：…` 只是非阻塞审查信息，不得描述为"
        "当前交付范围的交付成果、已实现能力或 User Impact。CI、Candidate、SHA、门禁和生命周期"
        "事实不得写入叙事。"
        + "\n\n不得包含 closing keywords。commit_message 与 pr_title 都必须各自采用 Conventional "
        "Commit 语义标题格式 `type: summary` 或 `type(scope): summary`，其中 type 只能是 "
        "feat、fix、improve、refactor、docs、test、chore；不要使用自然语言标题。"
        + "\n\n"
        + _publication_human_blocker_instruction()
    )


def _resume_recheck_instruction(context: dict[str, Any]) -> str:
    if "prior_human_blockers" not in context:
        return ""
    return (
        "这是一次 Human Blocker 恢复。prior_human_blockers 是上一轮未经改写的求助内容，"
        "human_response（如有）是维护者对此求助的未经改写回复；"
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
