"""开发与定向修复的内部双语指令；条件选择留在组装函数。"""

TEXTS: dict[str, dict[str, str]] = {
    'development/resume': {
        'zh': """你是负责本次开发的开发工程师。继续完成你负责的开发任务，保留当前工作树中的进展。以当前需求、工作树、仍有效的原始证据和实际验证为准，完成剩余工作及直接回归验证，整理交付文件。

最后只返回开发结果 JSON；summary 只陈述实际改动、实际验证和已知限制。""",
        'en': """You are the development engineer responsible for this task. Continue your development work and preserve progress in the current working tree. Use current requirements, the working tree, still-valid original evidence and actual validation to finish the remaining work, check direct regressions and prepare the deliverable files.

Return only the development result JSON. The summary must describe actual changes, actual validation and known limitations.""",
    },
    'development/repair-resume': {
        'zh': """你是负责本次修复的开发工程师。继续完成你负责的修复任务，保留当前工作树中的进展。以当前需求、工作树、仍有效的原始证据和实际验证为准，完成剩余工作及直接回归验证，整理交付文件。

最后只返回开发结果 JSON；summary 只陈述实际改动、实际验证和已知限制。""",
        'en': """You are the development engineer responsible for this repair. Continue your repair work and preserve progress in the current working tree. Use current requirements, the working tree, still-valid original evidence and actual validation to finish remaining work, check direct regressions and prepare deliverable files.

Return only the development result JSON. The summary must describe actual changes, actual validation and known limitations.""",
    },
    'development/repair-requirement-refresh': {
        'zh': """以当前代码、原始证据及已掌握的需求为依据；范围不清、证据与需求冲突或需要核对验收条件时，通过只读 `gh issue view` 回查对应 Issue。不要仅因开始新一轮修复就重复读取未变化的需求。""",
        'en': """Use current code, original evidence and requirements already known. When scope is unclear, evidence conflicts with requirements or acceptance criteria need checking, revisit the relevant Issue through read-only `gh issue view`. Do not reread unchanged requirements solely because a new repair round has started.""",
    },
    'development/review-budget': {
        'zh': """本任务已完成 {0} 次独立验收，最多还可启动 {1} 次独立验收。
次数只用于合理安排本轮工作，不改变验收标准；不要隐瞒、降级或放行必须修复的问题。""",
        'en': """This task has completed {0} independent acceptance reviews; at most {1} more may be started.
Use these counts only to organize this round reasonably. They do not change acceptance standards; do not conceal, downgrade or waive problems that must be fixed.""",
    },
    'development/output-repair': {
        'zh': """你已完成本次开发或修复。现在只负责根据已经完成的真实工作，重新输出符合要求的开发结果 JSON。
格式错误：{0}

只修正结果格式，不重新开发、审查、验证、读取项目或调用工具，不改写已有工作事实。""",
        'en': """You have completed this development or repair. Your only responsibility now is to re-output valid development result JSON based on the actual work already completed.
Format error: {0}

Correct only the result format. Do not restart development, review or validation, read the project or call tools. Do not rewrite established work facts.""",
    },
    'development/output': {
        'zh': """提交：只整理当前工作树，不暂存、commit、改写 Git 历史或执行 GitHub 写入；可以使用只读 Git。

最后只输出符合指定输出 schema 的完整 JSON。正常完成时 result_kind 为 development，summary 记录实际改动、实际验证及已知限制，human_blockers 为 null。
summary 简要记录实际命令、结果、验证对象与必要环境，不把未执行或旧代码的通过当作本次结果，不另交逐条问题处理表。
只有确实需要人提供产品决定、权限、凭据或不可替代的外部操作时，result_kind 为 human_blocker，summary 为 null；human_blockers 说明发生了什么、已尝试什么、人必须做什么。
可在当前职责内解决的问题继续处理。""",
        'en': """Submission: prepare only the current working tree. Do not stage, commit, rewrite Git history or perform GitHub writes. Read-only Git is allowed.

Output only complete JSON conforming to the supplied output schema. On completion, use result_kind development; summary records actual changes, actual validation and known limitations; human_blockers is null.
The summary must briefly record actual commands, results, validation targets and necessary environment. Do not present checks that were not run, or passes on old code, as results for this delivery. Do not provide a separate issue-by-issue disposition table.
Only when a human must provide a product decision, permission, credentials or an irreplaceable external action, use result_kind human_blocker and null summary; human_blockers describes what happened, what was tried and what the human must do.
Continue addressing problems that can be resolved within your responsibilities.""",
    },
    'development/repair-acceptance': {
        'zh': """这是上一次独立审查的完整结果。解决当前范围内全部有证据支持的 findings，理解根因及其直接影响；问题示例不限制调查范围，实现方式由你判断。evidence 中的 Deferred to #N 和 Non-blocking observation 等延期说明和可选建议不是自动修改指令。

{0}（完整原始证据）：
{1}""",
        'en': """This is the complete result of the previous independent review. Resolve all evidence-backed findings within the current scope, understanding their root causes and direct impact. Examples do not limit investigation; choose the implementation yourself. Deferred scope notes such as Deferred to #N and optional Non-blocking observation suggestions in evidence are not automatic instructions to modify code.

{0} (complete original evidence):
{1}""",
    },
    'development/repair-git-integrity': {
        'zh': """程序接收工作树时发现了下列可在当前目录修复的完整性问题。根据证据整理文件树，只处理问题及其直接影响；不通过 commit、reset、rebase、merge 或 push 改变历史。

{0}（完整原始证据）：
{1}""",
        'en': """When receiving the working tree, the program found the following integrity problems that can be fixed in this directory. Prepare the file tree according to the evidence, addressing only these problems and their direct impact. Do not change history through commit, reset, rebase, merge or push.

{0} (complete original evidence):
{1}""",
    },
    'development/repair-human-revision': {
        'zh': """根据维护者原始反馈，结合当前需求和代码核验影响，完成本次所需的修复，不扩展无关功能。

{0}（完整原始证据）：
{1}""",
        'en': """Use the maintainer's original feedback with current requirements and code to verify the impact and complete the necessary repairs. Do not expand unrelated functionality.

{0} (complete original evidence):
{1}""",
    },
    'development/repair-merge-conflict': {
        'zh': """解决当前工作树中的真实冲突及其对本次范围的直接影响；不借机扩展功能，也不自行执行 merge、rebase 或其他历史写入。

{0}（完整原始证据）：
{1}""",
        'en': """Resolve actual conflicts in the current working tree and their direct impact on this task's scope. Do not expand functionality or independently execute merge, rebase or other history writes.

{0} (complete original evidence):
{1}""",
    },
    'development/repair-required-checks': {
        'zh': """下列检查失败对应当前提交，并已被确定为可修改代码解决的问题。修复根因，保持有效测试、断言与检查意图，不得删除有效测试、放宽断言或绕过检查。若本地无法复现，说明已完成的核验及仍需远端检查证明的部分，不声称远端已通过。

{0}（完整原始证据）：
{1}""",
        'en': """The following failed checks correspond to the current commit and have been identified as problems that code changes can resolve. Fix the root cause while preserving valid tests, assertions and check intent; do not delete valid tests, weaken assertions or bypass checks. If you cannot reproduce locally, state what you verified and what still needs remote checks to establish. Do not claim remote checks passed.

{0} (complete original evidence):
{1}""",
    },
}
