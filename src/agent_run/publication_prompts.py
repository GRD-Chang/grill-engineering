"""提交说明与 PR 文案的只读角色合同。"""

from __future__ import annotations

from typing import Any

from agent_run.prompt_context import (
    human_continuation,
    pretty,
    task_brief,
    uses_short_role_prompt,
)


_PUBLICATION_CONTRACT = """只读当前工作区与证据，不修改文件、重新验收或执行 Git/GitHub 写入。
若在工作区外创建本轮临时文件，定位并清理，不进行宽泛删除。

正文面向未读过开发对话的读者，默认按以下四个部分组织：
- What Problem This Solves（解决什么问题）：说明具体使用场景、改动前的问题或限制，以及改动后的对应行为。
  必要时给出触发条件和前后对照，让读者理解这次改动解决了什么。
- Why This Change Was Made（为什么这样改）：解释采用当前实现方式的原因、关键设计决定和实际取舍，
  说明它们如何解决上述问题；重点写方案理由，无需复述开发过程。
- User Impact（对用户有什么影响）：说明哪些用户或维护者受到影响、他们会感受到什么变化，
  以及实际涉及的操作、兼容性、配置或迁移要求。没有用户可见变化时如实说明，并只描述实际影响。
- Evidence（验证依据）：列出已有证据中的实际测试或检查、结果及覆盖范围，必要时给出命令或报告引用。
  写清证据是否对应当前代码，以及尚未验证、失败或受阻的部分；旧代码的通过不能写成当前代码已通过。

按变更规模调整篇幅、合并章节，避免各部分重复；提交说明和 PR 标题默认使用 Conventional Commit 形式。
只陈述证据实际证明的事实，延期范围和可选建议不是本次交付成果。任务关联、关闭与完成信息由程序填写；
不写 closing keywords，也不预先宣称 CI、合并或尚未发生的验收已通过。内部提交身份和门禁信息不写入产品叙事。"""

_PUBLICATION_OUTPUT = """最后只输出：
{"result_kind":"publication","commit_message":"...","pr_title":"...","pr_body_markdown":"...","human_blockers":null}

只有确实需要人提供决定、权限、凭据或不可替代操作时，返回：
{"result_kind":"human_blocker","commit_message":null,"pr_title":null,"pr_body_markdown":null,"human_blockers":["发生了什么；已尝试什么；人必须做什么"]}"""


def publication_prompt(
    request: dict[str, Any], *, final_run: bool = False
) -> str:
    if final_run:
        request = _final_request(request)
    evidence = _publication_evidence(request)
    if uses_short_role_prompt(request):
        return publication_continuation_prompt(request)
    blocks = (
        "你是负责本次交付说明的工程师。根据需求、当前实际改动和下方证据，"
        "编写提交说明、PR 标题和正文。",
        task_brief(request, read_issues=True),
        evidence,
        human_continuation(request),
        _PUBLICATION_CONTRACT,
        _PUBLICATION_OUTPUT,
    )
    return "\n\n".join(block for block in blocks if block)


def publication_continuation_prompt(
    request: dict[str, Any], *, final_run: bool = False
) -> str:
    if final_run:
        request = _final_request(request)
    blocks = (
        "你是负责本次交付说明的工程师。继续完成你负责的交付说明。"
        "根据当前改动和允许使用的证据，完成准确的提交说明与 PR 文案。",
        task_brief(request, read_issues=False),
        _publication_evidence(request),
        human_continuation(request),
        "最后只返回文案结果 JSON，保留 result_kind、commit_message、pr_title、"
        "pr_body_markdown 和 human_blockers 字段。",
    )
    return "\n\n".join(block for block in blocks if block)


def _final_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request.get("acceptance_artifact"), dict):
        raise ValueError("Final Run Publication requires acceptance_artifact")
    return {
        **{key: value for key, value in request.items() if key != "fallback_publication_context"},
        "acceptance_scope": "run",
    }


def _publication_evidence(request: dict[str, Any]) -> str:
    fallback = request.get("fallback_publication_context")
    artifact = request.get("acceptance_artifact")
    if isinstance(fallback, dict):
        return (
            "本次可使用的验证事实：\n"
            "当前修改没有独立验收通过的结论；最近一次审查针对的是修改前的代码。\n"
            "以下为经过验证的发布凭据已有的最小事实（fallback_publication_context 原始 JSON）：\n"
            + pretty(fallback)
            + "\n\n区分上次审查对象及各维度结果、当前对象、后续修改、修复来源、"
            "失败证据来源与 Git 完整性结果。旧审查证据只适用于旧代码，后续修改信息只说明发生了什么改动；"
            "不能声称当前代码通过验收、问题已关闭、CI 已通过或已经允许合并。"
            "程序允许编写或发布文案不等于独立验收通过。"
        )
    if isinstance(artifact, dict):
        return (
            "本次可使用的验证事实：\n"
            "当前代码已获独立验收；只使用其中 e2e、standards、spec 三个维度的实际证据，"
            "不用开发自述代替验证。\n"
            "本次完整独立审查结果（acceptance_artifact 原始 JSON）：\n"
            + pretty(artifact)
        )
    raise ValueError(
        "Publication requires acceptance_artifact or fallback_publication_context"
    )
