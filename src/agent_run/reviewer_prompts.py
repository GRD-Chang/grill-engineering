"""独立验收的完整角色合同与同次执行续接。"""

from __future__ import annotations

from typing import Any

from agent_run.prompt_context import (
    human_continuation,
    pretty,
    prompt_context,
    review_budget,
    task_brief,
    uses_short_role_prompt,
)


_REVIEW_METHOD = """使用 skill:code-review。将上述任务作为它的需求来源，上述准确对象作为它的审查对象；
当目标包含未提交内容时，覆盖 Skill 默认只比较 HEAD 的步骤。
除 Skill 的 Standards/Spec 审查外，你还负责 E2E 验证，并对三个维度的最终结果负责。
所有审查或评价子 Agent 使用 fork_turns: "none"，只接收中立任务事实。"""

_READ_ONLY_VALIDATION = """以真实需求、当前代码、Git/只读 gh 和实际检查建立结论。开发总结、自测、开发侧审查和 PR 文案
不能代替你的独立验证。整个工作区保持只读；你可以启动服务、操作浏览器和创建测试数据，
构建缓存、运行数据、日志和浏览器产物等需要写入的内容使用工作区外仅服务本轮的可定位临时路径。
优先使用项目支持的外部缓存和运行目录；确实无法在只读目录运行时，允许在工作区外创建临时运行副本。
副本应对应当前工作区的实际待验收内容，包括未提交修改和未跟踪的交付文件，不能只复制 HEAD 的提交内容。
副本仅用于构建和运行验证，不修复或改写其中的交付源码、测试、配置及依赖声明；运行前后核对这些文件
与待验收内容一致，不能把修改副本后的通过作为原交付的验收结果。
结束前关闭本轮启动的服务和浏览器，清理临时副本及其他本轮临时产物。不得提交、推送、合并或修改 GitHub。"""

_E2E_VALIDATION = """E2E 根据本次需求和交付对象选择验证入口，确认从输入、操作到最终结果的关键流程能够完成：
- 可直接使用的功能：从实际使用入口完成关键操作。网页或浏览器扩展使用真实浏览器，命令行工具
  实际执行命令；核对用户可见结果及应产生的数据、文件等变化，不能只证明页面能打开或程序能启动。
- 库、代码接口或内部模块：通过实际接口或真实调用方，验证受影响的调用链和集成行为，核对输入、
  处理结果及必要的副作用。可使用现有测试或临时调用脚本，不在验证脚本中重新实现待测逻辑。
- 同时涉及上述两类内容时，覆盖各自受影响的路径；纯文档等不产生可执行功能的交付，按其实际用途
  核验内容与可使用性，无需人为增加浏览器操作或无关运行流程。

仅有代码阅读、局部单元测试或对关键路径的模拟结果，不足以证明上述流程已实际运行。
已有测试若实际覆盖对应入口和完整流程，可直接采用其本轮运行结果。依赖或环境限制使某段流程
无法验证时，如实说明未验证范围，不把局部通过写成端到端通过。
同时按适用仓库规范和测试指南完成当前交付要求的完整测试与必要检查。Standards 与 Spec 使用静态证据
和验证具体问题所需的检查，不重复相同完整套件，除非具体风险确实需要。
记录实际对象、验证入口、操作或命令、预期与实际结果、必要环境及适用时的退出码；未完成的检查不能写成通过。
使用临时运行副本时，在 evidence 中简述副本对应的验收对象和内容一致性核验情况。
代码、测试、依赖或环境变化后重新判断旧证据是否适用；失败时给出具体证据与复验要求，保留交付内容原样。"""

_FINDING_CONTRACT = """findings 中的问题会交回开发修复。只列本次范围内、有可复现且可定位证据、违反当前需求或硬性工程要求
或造成具体风险、使当前交付不可接受，并且可在当前任务内修复的问题。不要因修复量小而隐瞒真实缺陷。
同一根因合并为一条，写明问题、证据、所需修复及复验方式；一次报告已能证明的全部必要问题，
不为追求穷尽扩大任务范围，也不跨维度重复报告。

明确由其他任务承担的内容可在 evidence 中写“Deferred to #N：…”；有后续价值的可选建议可写
“Non-blocking observation：…”。二者不进入 findings，不改变状态，不触发自动修复；无实际价值的轻微意见省略。"""

_REVIEW_OUTPUT = """本次覆盖 code-review 默认的 Markdown 报告步骤，最后只输出：
{"checks":{"e2e":{"status":"...","evidence":"...","findings":[]},"standards":{"status":"...","evidence":"...","findings":[]},"spec":{"status":"...","evidence":"...","findings":[]}}}

每个 status 只取 pass、fail 或 blocked：有必须修复的问题时为 fail 且 findings 非空；
pass 与 blocked 的 findings 为空。确需人提供决定、权限或不可替代操作而无法形成结论时用 blocked，
在 evidence 中说明原因、已尝试的办法和人必须做什么。
e2e 的 evidence 说明实际操作及结果，standards 说明审查范围或基准，spec 说明验收条件及覆盖情况。
只有三个维度均 pass 才通过；不输出额外报告或问题处理对照表。"""


def review_prompt(request: dict[str, Any]) -> str:
    if uses_short_role_prompt(request, reviewer=True):
        return review_continuation_prompt(request)
    blocks = (
        "你是负责本次交付的独立验收工程师。实际验证下列交付是否满足对应需求，"
        "并审查相关代码；保持交付内容只读。",
        task_brief(request, read_issues=True),
        _review_object(request),
        _REVIEW_METHOD,
        _READ_ONLY_VALIDATION,
        _E2E_VALIDATION,
        _integration_evidence(request),
        _previous_review(request),
        review_budget(request, reviewer=True),
        human_continuation(request),
        _FINDING_CONTRACT,
        _REVIEW_OUTPUT,
    )
    return "\n\n".join(block for block in blocks if block)


def review_continuation_prompt(request: dict[str, Any]) -> str:
    blocks = (
        "你是负责本次交付的独立验收工程师。继续完成你负责的独立验收。"
        "以当前工作区和下列准确对象为准，完成尚未结束的核验。",
        task_brief(request, read_issues=False),
        _review_object(request),
        _integration_evidence(request),
        review_budget(request, reviewer=True),
        human_continuation(request),
        "最后只返回本次审查结果 JSON，保留 checks 中 e2e、standards、spec 三个维度的"
        " status、evidence 和 findings。",
    )
    return "\n\n".join(block for block in blocks if block)


def _review_object(request: dict[str, Any]) -> str:
    identity = request.get("current_review_identity")
    facts = identity if isinstance(identity, dict) else {}
    if request.get("acceptance_scope") != "run":
        description = "当前工作区对应待验收提交，检查比较基准到该提交的完整改动。"
    elif request.get("repair_scope") == "run_repair":
        description = (
            "当前工作区是本次修复与默认分支合并后的未提交预览；HEAD 留在默认分支基准是正常情况，"
            "检查修复进入整体后的结果。整体开发基准与实际合并基准可能不同，不把二者混为一谈。"
            "实际合并基准从当前工作区的只读 Git 取得。"
        )
    elif request.get("candidate_acceptance") is True:
        description = (
            "当前工作区是待验收修复与默认分支合并后的未提交预览；HEAD 留在默认分支基准是正常情况。"
            "按下面的实际默认分支基准和待合并提交检查工作树中的整体结果，不能只证明局部修复通过。"
        )
    else:
        description = (
            "当前工作区是默认分支与本次全部交付合并后的未提交预览。"
            "HEAD 保留在基准提交是正常情况；检查工作树中的整体结果。"
        )
    return "\n".join(
        ["本次验收对象：", description, *_identity_lines(facts)]
    )


def _identity_lines(identity: dict[str, Any]) -> list[str]:
    labels = (
        ("整体开发基准", "run_base_sha"),
        ("待合并的修复提交", "repair_candidate_sha"),
        ("默认分支合并基准", "default_base_sha"),
        ("本次全部交付的提交", "run_head_sha"),
        ("待合并提交", "candidate_sha"),
        ("比较基准", "reviewed_base_sha"),
        ("待验收提交", "reviewed_candidate_sha"),
        ("待验收文件树", "reviewed_candidate_tree"),
        ("预期合并文件树", "expected_merge_tree"),
    )
    return [
        f"{label}（{key}）：{identity[key]}"
        for label, key in labels
        if key in identity
    ]


def _previous_review(request: dict[str, Any]) -> str:
    artifact = request.get("previous_acceptance_artifact")
    if not isinstance(artifact, dict):
        return "按当前需求和代码检查本次完整范围，独立建立验收结论。"
    identity = request.get("previous_review_identity")
    facts = identity if isinstance(identity, dict) else {}
    return "\n".join(
        [
            "上一次验收对象：",
            *_identity_lines(facts),
            "",
            "上一次完整审查结果（previous_acceptance_artifact 原始 JSON）：",
            pretty(artifact),
            "",
            "优先复核原有问题、这次修改及其直接回归；没有具体风险依据时，"
            "不重复完整扫描未变化代码。当前证据或影响需要时可以扩大检查。"
            "你仍对本次完整对象负责，旧结果不能证明当前代码已经通过。",
        ]
    )


def _integration_evidence(request: dict[str, Any]) -> str:
    if request.get("acceptance_scope") != "run":
        return ""
    evidence = prompt_context(
        request, "fallback_ticket_records", "ticket_integration_records"
    )
    if not evidence:
        return ""
    return (
        "已有子任务的原始审查、后续修改与集成证据：\n"
        + pretty(evidence)
        + "\n\n这些记录帮助定位风险，不替代当前整体结果的独立验收。"
    )
