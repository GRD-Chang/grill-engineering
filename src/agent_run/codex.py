from __future__ import annotations

import inspect
import json
import math
import re
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
from agent_run.github_auth import (
    GitHubCredentialError,
    mint_read_only_installation_token,
)
from agent_run.worker_sandbox import (
    WorkerSandboxError,
    bubblewrap_command,
    run_worker_process,
    worker_environment,
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

    def __init__(
        self,
        executable: str = "codex",
        credential_provider: Callable[[], str] = mint_read_only_installation_token,
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
            mode = (
                f"Acceptance Repair：{subject}、代码状态和下方未经改写的 "
                "Acceptance Artifact 是事实依据。逐项处理 finding，保留原意，"
                "只修改 finding 及其直接影响范围，不改动已通过且不受影响的行为。"
            )
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
            mode = (
                f"Required-Checks Repair：{subject}、代码状态和下方未经改写的 "
                "CI Evidence 是事实依据。修复失败的 Required Checks 及其直接影响，"
                "不要绕过检查、删除测试或放宽断言。"
            )
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
            mode = (
                "Human Revision：维护者的下列反馈未经改写，是当前修复目标。"
                "先以当前代码和事实核验其影响，再完成必要的最小修复。"
            )
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
            mode = (
                "Merge Conflict Repair：默认分支与 Run Branch 的真实合并预览失败。"
                "在不绕过既有验收的前提下修复冲突及其直接影响。"
            )
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
            + _issue_context_instruction(_development_context(request))
            + _human_blocker_instruction()
            + "\n\n"
            "阅读适用的 AGENTS.md、相关实现、测试和真实调用入口；在适合的位置尽量"
            "采用 TDD。运行相关单测、typecheck、lint 和完整测试套件，并从真实用户"
            "入口复验受影响的成功路径、失败路径和边界情况。记录实际命令、exit code、"
            "可观察结果和必要状态变化；不要用 mock、单元测试或代码阅读替代能够真实"
            "运行的核心路径。\n\n"
            "完成实现和使用验证后，必须使用 skill:code-review 派发两个不同 "
            "subagent：Standards Review Subagent 检查仓库标准以及具体 correctness、"
            "security、regression 和 maintainability 问题；Spec Review Subagent "
            "检查 Acceptance Criteria 是否完整实现、是否错误实现或存在有实际影响的 "
            "scope creep。你不得自行宣布必要审查通过。发现 blocking finding 后必须"
            "修复、重跑受影响测试和真实路径，并重新取得受影响 subagent 的有效复查。"
            "\n\nPublisher 是唯一 Mutation Authority；不要 commit、push、merge、"
            "close 或修改 PR/Issue。Run Repair 不得关闭 Ticket、创建 Ticket PR 或使用"
            "Ticket 的修改预算。开发侧验证不是正式 Acceptance。最后只用普通文本"
            "总结改动、实际验证、两个审查结果和剩余 blocker。最后只输出完整 Development "
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
            "你是本次 Final Run 的发布叙事工程师。先用 `gh issue view` 读取 "
            "parent_issue_url 指向的 Parent Issue；它定义整体交付目标。然后在当前 "
            "checkout 中阅读实际累计 diff。只读取事实，生成最终 Run PR 的语义标题和正文；"
            "不要编辑文件、不要执行 Git/GitHub 写操作，也不要做验收或替代人工批准。"
            "必须包含非空 What Problem This Solves、Why This Change Was Made、User Impact、"
            "Evidence 四个二级标题；不得包含 closing keywords。commit_message 与 pr_title "
            "必须采用 Conventional Commit 语义标题格式 `type: summary` 或 `type(scope): summary`，"
            "type 只能是 feat、fix、improve、refactor、docs、test、chore。"
            + _publication_human_blocker_instruction()
            + _resume_recheck_instruction(context)
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
                "你是本次 Run Repair 的发布叙事工程师。先用 `gh issue view` 读取 "
                "parent_issue_url 指向的 Parent Issue，再依据当前 checkout 的实际 diff 和"
                "下方完整独立验收证据，输出小型 Publication Artifact。"
                "不要修改文件，也不要执行任何 Git/GitHub 写操作。Parent Issue、"
                "Delivery Type、Delivery Run、SHA、CI 与生命周期事实由 Publisher 注入，"
                "叙事中不得输出这些字段；并包含四个非空二级标题："
                "What Problem This Solves、Why This Change Was Made、User Impact、Evidence。"
                "禁止 closing keywords。commit_message 与 pr_title 都必须各自采用 Conventional "
                "Commit 语义标题格式 `type: summary` 或 `type(scope): summary`，其中 type 只能是 "
                "feat、fix、improve、refactor、docs、test、chore；不要使用自然语言标题。\n\n"
                + _publication_human_blocker_instruction()
                + _resume_recheck_instruction(context)
                + "\n\n"
                + artifact_input
                + "\n\nPublication Context:\n"
                + _pretty(context)
            )
        return (
            "你是本次交付的发布叙事工程师。先用 `gh issue view` 读取 parent_issue_url；"
            "若给出 task_issue_url，也必须读取该当前 Ticket。然后依据 checkout 的实际 diff 和"
            "下方完整独立验收证据，输出小型 Publication Artifact。"
            "不要修改文件，也不要执行任何 Git/GitHub 写操作。PR 叙事包含四个非空二级标题："
            "What Problem This Solves、Why This Change Was Made、User Impact、Evidence。"
            "只描述已经发生的真实验证；不要把开发者自述当作验证事实。Parent Issue、"
            "Primary Ticket、Delivery Type、Delivery Run、SHA、CI 与生命周期事实由 Publisher"
            "注入，叙事中不得输出这些字段。禁止 closing keywords。commit_message 与 pr_title 都必须各自采用 Conventional "
            "Commit 语义标题格式 `type: summary` 或 `type(scope): summary`，其中 type 只能是 "
            "feat、fix、improve、refactor、docs、test、chore；不要使用自然语言标题。\n\n"
            + _publication_human_blocker_instruction()
            + _resume_recheck_instruction(context)
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
            initial_writable_checkout=True,
        )
        return ReviewResult(
            thread_id=thread_id,
            artifact=_json_object(output, "Acceptance Artifact"),
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
                output, reported_thread = self._invoke(
                    prompt=attempt_prompt,
                    checkout=checkout,
                    thread_id=current_thread,
                    schema=schema,
                    writable_checkout=initial_writable_checkout and attempt == 1,
                    on_thread=lambda value: notify(
                        "thread_started",
                        reported_thread_id=value,
                        attempt_count=attempt,
                    ),
                )
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
    def _review_prompt(request: dict[str, Any]) -> str:
        context = _prompt_context(
            request,
            "parent_issue_url",
            "task_issue_url",
            "prior_human_blockers",
            "human_response_history",
        )
        scope_instruction = (
            "这是 Run Acceptance：从 Parent Issue 和 GitHub 独立读取最终 Ticket Set 与"
            "依赖关系，并检查准备好的累计 diff 及整体验收标准；不要寻找或猜测 Controller "
            "私有账本，也不要把单 Ticket 通过当成整体验收通过。"
            if request.get("acceptance_scope") == "run"
            else "这是 Parent-only Fresh Acceptance：以当前 Parent Issue 的完整验收标准为范围。"
            if request.get("acceptance_scope") == "parent_only"
            else "这是 Ticket Fresh Acceptance：以当前 Ticket 的完整验收标准为范围。"
        )
        prompt = (
            "你是全新且独立的 Fresh Validation 工程师。不要依赖开发者总结、自测、"
            "开发审查、PR 文案或 Publication Artifact；先用 `gh issue view` 读取 "
            "parent_issue_url，若给出 task_issue_url 也读取它，并使用真实 Git/gh 自行建立"
            "事实。必须派发三个不同 subagent：一个真实执行 E2E 使用；一个使用 "
            "skill:code-review 执行 Standards Review；另一个使用 "
            "skill:code-review 执行 Spec Review。你不得替代任何缺失 lane 或自行"
            "签署通过；subagent 失败时必须解决派发问题并重新派发。Validation "
            "Checkout 可写，允许构建和测试中间产物。汇总三条 lane 的实际证据后，"
            "只输出符合 schema 的 Acceptance Artifact。可修复问题写入自包含 "
            "findings。human 必须克制，仅限确实需要产品决策、外部权限、敏感凭证"
            "或不可替代外部操作的阻塞。若 verdict 为 pass，三个 check 都必须是 pass，"
            "findings 与 human_blockers 必须都是空数组 `[]`；不要输出提示、风格建议、"
            "未来改进或其他非阻塞观察。\n\n"
            + _resume_recheck_instruction(context)
            + "\n\n"
            f"{scope_instruction}\n\n"
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
            if schema is not None:
                codex_arguments.extend(["--output-schema", str(schema_path)])
            codex_arguments.extend(
                ["--output-last-message", str(output_path), "-"]
            )
            try:
                github_read_token = self.credential_provider()
            except GitHubCredentialError as error:
                raise CodexProcessError(str(error)) from error
            if not github_read_token:
                raise CodexProcessError(
                    "GitHub credential provider returned an empty token"
                )
            environment = worker_environment(
                temporary / "gh", github_read_token
            )
            try:
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
                    "timeout": 3600,
                }
                if on_thread is not None and "on_stdout_line" in inspect.signature(
                    run_worker_process
                ).parameters:
                    worker_options["on_stdout_line"] = _thread_line_callback(
                        expected=thread_id, callback=on_thread
                    )
                result = run_worker_process(arguments, **worker_options)
            except WorkerSandboxError as error:
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
    clean = "".join(character for character in value if character >= " " or character in "\n\t")
    clean = re.sub(
        r"(?i)\b(authorization|proxy-authorization)"
        r"([\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic)\s+)?"
        r"([^\s,;\"'}]+)",
        r"\1\2[REDACTED]",
        clean,
    )
    clean = re.sub(
        r"(?i)\b(token|api[_-]?key|secret|password)"
        r"([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)",
        r"\1\2[REDACTED]",
        clean,
    )
    encoded = clean.encode("utf-8")[:8192]
    return encoded.decode("utf-8", errors="ignore")


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


def _issue_context_instruction(context: dict[str, Any]) -> str:
    task_instruction = (
        " task_issue_url 是当前立即工作 Ticket，开始前也必须通过 `gh issue view` 读取它。"
        if "task_issue_url" in context
        else ""
    )
    return (
        "动态 Context 中的 parent_issue_url 是定义整体交付目标的 Parent Issue，开始前"
        "必须通过只读 `gh issue view` 读取它；URL 不是需求摘要。"
        + task_instruction
        + _resume_recheck_instruction(context)
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
