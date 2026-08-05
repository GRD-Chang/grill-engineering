from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Callable

from agent_run.agents import DevelopmentResult, PublicationResult, ReviewResult
from agent_run.agent_schemas import acceptance_schema, publication_schema
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
    pass


class _CodexThreadResumeError(CodexProcessError):
    pass


class CodexCliBackend:
    """Runs untrusted Codex workers without Publisher GitHub credentials."""

    def __init__(
        self,
        executable: str = "codex",
        credential_provider: Callable[[], str] = mint_read_only_installation_token,
    ) -> None:
        self.executable = executable
        self.credential_provider = credential_provider

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        checkout = Path(_string(request, "checkout"))
        thread_id = request.get("thread_id")
        prompt = self._development_prompt(request)
        resumed_thread = str(thread_id) if isinstance(thread_id, str) else None
        replaced_thread: str | None = None
        try:
            output, actual_thread = self._invoke(
                prompt=prompt,
                checkout=checkout,
                thread_id=resumed_thread,
            )
        except _CodexThreadResumeError:
            if resumed_thread is None:
                raise
            replaced_thread = resumed_thread
            output, actual_thread = self._invoke(
                prompt=(
                    "旧 Development Thread 恢复失败。你是接替该工作的 Development "
                    "Codex；下面的 Development Brief 是完整恢复上下文，请在同一 "
                    "Ticket Job、branch 和 PR 上继续，不要重新规划或丢失未解决证据。"
                    "\n\n"
                    + prompt
                ),
                checkout=checkout,
                thread_id=None,
            )
        return DevelopmentResult(
            thread_id=actual_thread,
            summary=output.strip(),
            replaced_thread_id=replaced_thread,
        )

    @staticmethod
    def _development_prompt(request: dict[str, Any]) -> str:
        is_run_repair = request.get("acceptance_scope") == "run"
        repair_source = request.get("repair_source")
        if repair_source is None:
            mode = (
                "Development Brief：以当前 Ticket 和代码事实为依据，以最小、完整、"
                "可维护的改动满足全部 Acceptance Criteria。"
            )
            heading = "Development Brief"
            prompt_input = _pretty(request)
        elif repair_source == "acceptance":
            artifact = request.get("acceptance_artifact")
            if not isinstance(artifact, dict):
                raise ValueError(
                    "Acceptance Repair requires acceptance_artifact"
                )
            subject = "当前 Delivery Run" if is_run_repair else "当前 Ticket"
            mode = (
                f"Acceptance Repair：{subject}、代码状态和下方未经改写的 "
                "Acceptance Artifact 是事实依据。逐项处理 finding，保留原意，"
                "只修改 finding 及其直接影响范围，不改动已通过且不受影响的行为。"
            )
            heading = "Acceptance Repair Input"
            context = dict(request)
            context.pop("acceptance_artifact")
            prompt_input = (
                f"Acceptance Artifact (verbatim JSON):\n{_pretty(artifact)}"
                f"\n\nDevelopment Brief:\n{_pretty(context)}"
            )
        elif repair_source == "required_checks":
            evidence = request.get("ci_evidence")
            if not isinstance(evidence, dict):
                raise ValueError("Required-Checks Repair requires ci_evidence")
            mode = (
                "Required-Checks Repair：当前 Ticket、代码状态和下方未经改写的 "
                "CI Evidence 是事实依据。修复失败的 Required Checks 及其直接影响，"
                "不要绕过检查、删除测试或放宽断言。"
            )
            heading = "Required-Checks Repair Input"
            context = dict(request)
            context.pop("ci_evidence")
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
            context = dict(request)
            context.pop("human_feedback")
            prompt_input = (
                f"Maintainer Feedback (verbatim):\n{feedback.strip()}"
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
            context = dict(request)
            context.pop("merge_conflict_evidence")
            prompt_input = (
                f"Merge Conflict Evidence (verbatim):\n{evidence.strip()}"
                f"\n\nDevelopment Brief:\n{_pretty(context)}"
            )
        else:
            raise ValueError(f"unknown repair_source: {repair_source}")

        role = "本次 Delivery Run 的修复工程师" if is_run_repair else "当前 Ticket 的开发工程师"
        return (
            f"你是负责{role}。使用 skill:implement 完成开发或修复。"
            f"{mode}\n\n"
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
            "总结改动、实际验证、两个审查结果和剩余 blocker。\n\n"
            f"{heading}:\n{prompt_input}"
        )

    def publication(self, request: dict[str, Any]) -> PublicationResult:
        checkout = Path(_string(request, "checkout"))
        thread_id = _string(request, "thread_id")
        prompt = self._publication_prompt(request)
        replaced_thread: str | None = None
        try:
            output, resumed_thread = self._invoke(
                prompt=prompt,
                checkout=checkout,
                thread_id=thread_id,
                schema=publication_schema(),
                writable_checkout=False,
            )
        except _CodexThreadResumeError:
            replaced_thread = thread_id
            output, resumed_thread = self._invoke(
                prompt=(
                    "旧发布叙事 Agent 恢复失败。你是接替该工作的发布叙事工程师；"
                    "下面的 Publication Brief 是完整恢复上下文。保留同一 Candidate，"
                    "不要重新开发、改动文件或改变发布范围。\n\n"
                    + prompt
                ),
                checkout=checkout,
                thread_id=None,
                schema=publication_schema(),
                writable_checkout=False,
            )
        return PublicationResult(
            thread_id=resumed_thread,
            artifact=_json_object(output, "Publication Artifact"),
            replaced_thread_id=replaced_thread,
        )

    def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
        """Create final-PR prose in a new, read-only, one-shot worker."""
        checkout = Path(_string(request, "checkout"))
        run_id = _string(request, "run_id")
        prompt = (
            "你是一次性的 Run Publication Codex。只读取当前事实，生成最终 Run PR "
            "的语义标题和正文；不要编辑文件、不要执行 Git/GitHub 写操作，也不要做 "
            "验收或替代人工批准。Parent Issue、Delivery Type、Delivery Run、SHA、CI 与"
            "生命周期事实由 Publisher 注入，叙事中不得输出这些字段；并包含非空 "
            "What Problem This Solves、Why This Change Was Made、User Impact、Evidence、"
            "Completed Tickets、Known Limitations、Validation Results 七个二级标题；"
            "不得包含 closing keywords。commit_message 与 pr_title 都必须各自采用 "
            "Conventional Commit 语义标题格式 `type: summary` 或 `type(scope): summary`，"
            "其中 type 只能是 feat、fix、improve、refactor、docs、test、chore；"
            "不要使用自然语言标题。\n\n"
            f"Run Publication Brief:\n{_pretty(request)}"
        )
        output, _thread_id = self._invoke(
            prompt=prompt,
            checkout=checkout,
            thread_id=None,
            schema=publication_schema(),
            writable_checkout=False,
        )
        return _json_object(output, "Run Publication Artifact")

    @staticmethod
    def _publication_prompt(request: dict[str, Any]) -> str:
        if request.get("acceptance_scope") == "run":
            return (
                "你是本次 Run Repair 的发布叙事工程师。当前 Candidate 已通过独立验收；"
                "根据当前累计 diff、开发摘要和独立验收证据，输出小型 Publication Artifact。"
                "不要修改文件，也不要执行任何 Git/GitHub 写操作。Parent Issue、"
                "Delivery Type、Delivery Run、SHA、CI 与生命周期事实由 Publisher 注入，"
                "叙事中不得输出这些字段；并包含四个非空二级标题："
                "What Problem This Solves、Why This Change Was Made、User Impact、Evidence。"
                "禁止 closing keywords。commit_message 与 pr_title 都必须各自采用 Conventional "
                "Commit 语义标题格式 `type: summary` 或 `type(scope): summary`，其中 type 只能是 "
                "feat、fix、improve、refactor、docs、test、chore；不要使用自然语言标题。\n\n"
                f"Publication Brief:\n{_pretty(request)}"
            )
        return (
            "你是本次交付的发布叙事工程师。当前 Candidate 已通过独立验收；"
            "根据当前累计 diff、开发摘要和独立验收证据，输出小型 Publication Artifact。"
            "不要修改文件，也不要执行任何 Git/GitHub 写操作。PR 叙事包含四个非空二级标题："
            "What Problem This Solves、Why This Change Was Made、User Impact、Evidence。"
            "只描述已经发生的真实验证；不要把开发者自述当作验证事实。Parent Issue、"
            "Primary Ticket、Delivery Type、Delivery Run、SHA、CI 与生命周期事实由 Publisher"
            "注入，叙事中不得输出这些字段。禁止 closing keywords。commit_message 与 pr_title 都必须各自采用 Conventional "
            "Commit 语义标题格式 `type: summary` 或 `type(scope): summary`，其中 type 只能是 "
            "feat、fix、improve、refactor、docs、test、chore；不要使用自然语言标题。\n\n"
            f"Publication Brief:\n{_pretty(request)}"
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        checkout = Path(_string(request, "checkout"))
        scope_instruction = (
            "这是 Run Acceptance：必须检查 Parent Issue、最终 Ticket Set 与依赖图、"
            "每张 Ticket Completion Record、基线到 Run Branch Head 的累计 diff，以及"
            "Expected Merge Result；不要把单 Ticket 通过当成整体验收通过。"
            if request.get("acceptance_scope") == "run"
            else "这是 Ticket Fresh Acceptance：以当前 Ticket 的完整验收标准为范围。"
        )
        prompt = (
            "你是全新且独立的 Fresh Validation 工程师。不要依赖开发者总结、自测、"
            "开发审查、PR 文案或 Publication Artifact；使用真实 Git/gh、Ticket、Parent Issue 和准确 SHA 自行建立"
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
            f"{scope_instruction}\n\n"
            f"Acceptance Brief:\n{_pretty(request)}"
        )
        output, thread_id = self._invoke(
            prompt=prompt,
            checkout=checkout,
            thread_id=None,
            schema=acceptance_schema(),
        )
        return ReviewResult(
            thread_id=thread_id,
            artifact=_json_object(output, "Acceptance Artifact"),
        )

    def assess_scope(self, request: dict[str, Any]) -> dict[str, Any]:
        checkout = Path(_string(request, "checkout"))
        prompt = (
            "你是一次性的 Scope Impact Assessment Codex Worker。比较新旧 "
            "Parent Spec、当前 Ticket Graph 和既有已完成工作，只判断 Parent 变化"
            "是否改变 Ticket 集合、依赖关系、整体交付边界，或使已完成 Ticket 需要"
            "返工。文案澄清或不影响这些结构的补充不是结构性变化。使用所需工具核验，"
            "但不要 commit、push、merge、close 或修改 Issue/PR；只输出符合 schema "
            "的结构化判断。\n\n"
            f"Scope Assessment Brief:\n{_pretty(request)}"
        )
        output, _thread_id = self._invoke(
            prompt=prompt,
            checkout=checkout,
            thread_id=None,
            schema=_scope_impact_schema(),
        )
        return _json_object(output, "Scope Impact Assessment")

    def _invoke(
        self,
        *,
        prompt: str,
        checkout: Path,
        thread_id: str | None,
        schema: dict[str, Any] | None = None,
        writable_checkout: bool = True,
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
                result = run_worker_process(
                    arguments,
                    cwd=checkout,
                    prompt=prompt,
                    environment=environment,
                    timeout=3600,
                )
            except WorkerSandboxError as error:
                raise CodexProcessError(str(error)) from error
            if result.returncode != 0:
                message = result.stderr.strip() or "Codex worker failed"
                if thread_id is not None:
                    raise _CodexThreadResumeError(message)
                raise CodexProcessError(message)
            if not output_path.exists():
                raise CodexProcessError("Codex worker did not produce a final response")
            actual_thread = _thread_id(result.stdout) or thread_id
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


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _scope_impact_schema() -> dict[str, Any]:
    text_fields = (
        "summary",
        "ticket_set_impact",
        "dependency_impact",
        "delivery_boundary_impact",
        "completed_work_impact",
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["structural_change", *text_fields],
        "properties": {
            "structural_change": {"type": "boolean"},
            **{
                field: {"type": "string", "minLength": 1}
                for field in text_fields
            },
        },
    }
