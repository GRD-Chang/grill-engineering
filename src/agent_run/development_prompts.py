"""Implementation and directed-repair instructions selected by the caller."""

from __future__ import annotations

from typing import Any

from agent_run.prompt_context import (
    human_continuation,
    pretty,
    review_budget,
    task_brief,
    uses_short_role_prompt,
)


_VALIDATION = (
    "验证：交付结果的后续审查和最终完整测试，由另一位验收工程师负责。"
    "你本轮按修改的实际风险验证受影响功能，无需每轮固定运行完整测试；"
    "影响范围不明或发现具体风险时仍应扩大验证。"
)
_INITIAL_REVIEW = (
    "审查：实现、验证和自行检查后，建议安排审查子 Agent 对当前交付进行一轮检查。"
    "本次开发最多组织一轮，可按实际风险安排不同方向的审查子 Agent。"
    "处理本轮问题后自行检查和复测，不反复启动通用审查。"
)
_REPAIR_REVIEW = (
    "审查：默认不再组织独立审查；只有修复既改变原方案、又引入此前未覆盖的关键风险时，"
    "才允许一次针对该风险的审查。处理结果后自行检查和复测，不反复启动通用审查。"
)
_SUBAGENTS = (
    '派发审查子 Agent 时使用 fork_turns: "none"，提供中立的需求、范围和代码事实，不传递预设结论。'
    "审查使用 skill:code-review 的 Standards/Spec 方法，检查当前工作树，包括未提交修改与"
    "未跟踪的交付文件，覆盖其默认只比较已提交 HEAD 的做法。探索或并行实现等其他协作按任务需要组织。"
)
_GIT = (
    "提交：只整理当前工作树，不暂存、commit、改写 Git 历史或执行 GitHub 写入；可以使用只读 Git。"
)
_HYGIENE = (
    "程序会统一提交当前工作区的修改和未被忽略的新增文件。结束前检查全部未提交内容，保留应交付的"
    "代码、测试、文档和配置，清理本次生成的临时、构建和测试产物。只有长期可再生且不应版本控制的"
    "项目产物才适合加入 .gitignore，不用忽略规则隐藏交付文件。工作区外的本轮临时路径也要定位并清理，"
    "不进行宽泛删除。"
)
_COMPACT_REVIEW = (
    '使用 fork_turns: "none" 和中立任务事实，按 skill:code-review 方法检查当前工作树及未跟踪交付文件。'
    "这替代 implement 的固定结束审查步骤。"
)
_COMPACT_DELIVERY = (
    "程序会统一提交工作树的修改和未忽略的新增文件；检查全部未提交内容，保留交付文件，清理本轮临时产物，"
    "不以 .gitignore 隐藏交付。只整理文件，不暂存、commit、改写 Git 历史或执行 GitHub 写入。"
    "工作区外的本轮临时路径同样要定位并清理，不进行宽泛删除。"
)
_OUTPUT = (
    "最后只输出完整 JSON：\n"
    '`{"result_kind":"development","summary":"实际改动、实际验证及已知限制","human_blockers":null}`\n'
    "summary 简要记录实际命令、结果、验证对象与必要环境，不把未执行或旧代码的通过当作本次结果，"
    "不另交逐条问题处理表。完整测试失败时先定向定位和修复，再对收口后的代码复验，不每改一处就跑全量。\n"
    "只有确实需要人提供产品决定、权限、凭据或不可替代的外部操作时，返回：\n"
    '`{"result_kind":"human_blocker","summary":null,"human_blockers":["发生了什么；已尝试什么；人必须做什么"]}`\n'
    "可在当前职责内解决的问题继续处理。"
)

_REPAIR_SOURCES = {
    "acceptance": (
        "acceptance_artifact",
        "Acceptance Repair",
        "这是上一次独立审查的完整结果。解决当前范围内全部有证据支持的 findings，理解根因及其直接影响；"
        "问题示例不限制调查范围，实现方式由你判断。evidence 中的 Deferred to #N 和 "
        "Non-blocking observation 等延期说明和可选建议不是自动修改指令。",
    ),
    "git_integrity": (
        "git_integrity_evidence",
        "Git Integrity Repair",
        "程序接收工作树时发现了下列可在当前目录修复的完整性问题。根据证据整理文件树，只处理问题及其直接影响；"
        "不通过 commit、reset、rebase、merge 或 push 改变历史。",
    ),
    "required_checks": (
        "ci_evidence",
        "Required-Checks Repair",
        "下列检查失败对应当前提交，并已被确定为可修改代码解决的问题。修复根因，保持有效测试、断言与检查意图，"
        "不得删除有效测试、放宽断言或绕过检查。若本地无法复现，说明已完成的核验及仍需远端检查证明的部分，"
        "不声称远端已通过。",
    ),
    "human_revision": (
        "human_feedback",
        "Human Revision",
        "根据维护者原始反馈，结合当前需求和代码核验影响，完成本次所需的修复，不扩展无关功能。",
    ),
    "merge_conflict": (
        "merge_conflict_evidence",
        "Merge Conflict Repair",
        "解决当前工作树中的真实冲突及其对本次范围的直接影响；不借机扩展功能，也不自行执行 merge、"
        "rebase 或其他历史写入。",
    ),
}


def _repair_evidence(request: dict[str, Any]) -> tuple[str, str]:
    source = request.get("repair_source")
    if source is None:
        return "", ""
    if not isinstance(source, str) or source not in _REPAIR_SOURCES:
        raise ValueError(f"unknown repair_source: {source}")
    field, error_role, instruction = _REPAIR_SOURCES[source]
    evidence = request.get(field)
    if source in {"human_revision", "merge_conflict"}:
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"{error_role} requires {field}")
        raw = evidence
    else:
        if not isinstance(evidence, dict):
            raise ValueError(f"{error_role} requires {field}")
        raw = pretty(evidence)
    return instruction, f"{field}（完整原始证据）：\n{raw}"


def development_prompt(
    request: dict[str, Any], *, force_continuation: bool = False
) -> str:
    repair = request.get("repair_source") is not None
    source_instruction, evidence = _repair_evidence(request)
    if force_continuation or uses_short_role_prompt(request):
        work = "修复" if repair else "开发"
        return "\n\n".join(
            block
            for block in (
                f"你是负责本次{work}的开发工程师。继续完成你负责的{work}任务，保留当前工作树中的进展。"
                "以当前需求、工作树、仍有效的原始证据和实际验证为准，完成剩余工作及直接回归验证，整理交付文件。",
                task_brief(request, read_issues=False, development=True),
                source_instruction,
                evidence,
                review_budget(request, reviewer=False) if repair else "",
                human_continuation(request),
                "最后只返回开发结果 JSON；summary 只陈述实际改动、实际验证和已知限制。",
            )
            if block
        )

    thread = request.get("thread_id")
    compact = (
        repair
        and isinstance(thread, str)
        and bool(thread.strip())
        and request.get("_invocation_mode") != "new-thread"
    )
    work = "修复" if repair else "实现"
    completion = (
        "解决本次有证据支持的问题，验证根因、直接影响的同类场景和本次修复可能造成的回归，"
        "并留下适合提交的文件树。"
        if repair
        else "实现本次全部验收条件，验证直接影响的路径，处理已知的当前范围问题，并留下适合提交的文件树。"
    )
    blocks = [
        f"你是负责{work}本次任务的开发工程师。使用 skill:implement，在当前工作区完成{work}、验证并整理交付文件。",
        task_brief(request, read_issues=not compact, development=True),
        source_instruction,
        evidence,
        (
            "以当前代码、原始证据及已掌握的需求为依据；范围不清、证据与需求冲突或需要核对验收条件时，"
            "通过只读 `gh issue view` 回查对应 Issue。不要仅因开始新一轮修复就重复读取未变化的需求。"
            if compact else ""
        ),
        (
            "\n".join((_VALIDATION, _REPAIR_REVIEW, _COMPACT_REVIEW))
            if compact
            else "本次对 implement 的结束步骤作以下调整：\n- "
            + "\n- ".join((_VALIDATION, _REPAIR_REVIEW if repair else _INITIAL_REVIEW, _GIT))
        ),
        "" if compact else _SUBAGENTS,
        _COMPACT_DELIVERY if compact else _HYGIENE,
        "完成条件：" + completion + "你只报告实际开发与自测结果；交付是否通过审查，由验收工程师另行判断。",
        review_budget(request, reviewer=False) if repair else "",
        human_continuation(request),
        _OUTPUT,
    ]
    return "\n\n".join(block for block in blocks if block)
