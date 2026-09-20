"""发布文案的双语证据边界、续接与输出语义。"""

TEXTS: dict[str, dict[str, str]] = {
    'publication/fallback-evidence': {
        'zh': """本次可使用的验证事实：
当前修改没有独立验收通过的结论；最近一次审查针对的是修改前的代码。
以下为经过验证的发布凭据已有的最小事实（fallback_publication_context 原始 JSON）：
{}

区分上次审查对象及各维度结果、当前对象、后续修改、修复来源、失败证据来源与 Git 完整性结果。旧审查证据只适用于旧代码，后续修改信息只说明发生了什么改动；不能声称当前代码通过验收、问题已关闭、CI 已通过或已经允许合并。程序允许编写或发布文案不等于独立验收通过。""",
        'en': """Validation facts available for this task:
The current changes have no passing independent acceptance conclusion; the most recent review covered the code before these changes.
The following are the minimal facts already present in the validated publication receipt (original fallback_publication_context JSON):
{}

Distinguish the previous review object and each dimension's result from the current object, subsequent changes, repair sources, failure-evidence sources and Git integrity results. Previous review evidence applies only to the previous code; subsequent change information describes only what changed. Do not claim that current code passed acceptance review, findings are closed, CI passed or merging is authorized. Permission from the program to draft or publish copy does not mean independent acceptance review passed.""",
    },
    'publication/accepted-evidence': {
        'zh': """本次可使用的验证事实：
当前代码已获独立验收；只使用其中 e2e、standards、spec 三个维度的实际证据，不用开发自述代替验证。
本次完整独立审查结果（acceptance_artifact 原始 JSON）：""",
        'en': """Validation facts available for this task:
The current code has passed independent acceptance review. Use only the actual evidence in its e2e, standards and spec dimensions; do not substitute development claims for validation.
Complete independent review result for this task (original acceptance_artifact JSON):""",
    },
    'publication/handshake': {
        'zh': """你是负责提交说明输出格式检查的工程师。本次不读取或修改仓库，不调用工具。仅返回 result_kind 为 human_blocker，commit_message、pr_title 和 pr_body_markdown 为 null，human_blockers 为只含一条非空中文字符串的数组。""",
        'en': """You are the engineer responsible for checking the commit-description output format. Do not read or modify the repository or call tools. Return only result_kind set to human_blocker, commit_message, pr_title and pr_body_markdown set to null, and human_blockers set to an array containing exactly one nonempty English string.""",
    },
    'publication/output': {
        'zh': """只读当前工作区与证据，不修改文件、重新验收或执行 Git/GitHub 写入。
若在工作区外创建本轮临时文件，定位并清理，不进行宽泛删除。

任务关联、关闭与完成信息由程序填写；不写 closing keywords，也不预先宣称 CI、合并或尚未发生的验收已通过。

最后只输出符合指定输出 schema 的 JSON。正常完成时 result_kind 为 publication，提供 commit_message、pr_title 和 pr_body_markdown，human_blockers 为 null。

只有确实需要人提供决定、权限、凭据或不可替代操作时，result_kind 为 human_blocker，三个文案字段为 null；human_blockers 说明发生了什么、已尝试什么、人必须做什么。""",
        'en': """Read only the current workspace and evidence. Do not modify files, repeat acceptance review or perform Git/GitHub writes.
If you create temporary files outside the workspace for this task, locate and clean them up; do not perform broad deletion.

The program supplies task associations, closure and completion information. Do not write closing keywords or prematurely claim that CI, merging or acceptance review that has not happened has passed.

Output only JSON conforming to the supplied output schema. On completion use result_kind publication, provide commit_message, pr_title and pr_body_markdown, and set human_blockers to null.

Only when a human must provide a decision, permission, credentials or an irreplaceable action, use result_kind human_blocker and null for the three copy fields; human_blockers describes what happened, what was tried and what the human must do.""",
    },
    'publication/output-repair': {
        'zh': """你已完成本次交付说明编写。现在只负责根据已经完成的真实工作，重新输出符合要求的文案结果 JSON。
格式错误：{0}

只修正结果格式，不重新开发、审查、验证、读取项目或调用工具，不改写已有工作事实。""",
        'en': """You have completed this delivery-description work. Your only responsibility now is to re-output valid publication copy result JSON based on the actual work already completed.
Format error: {0}

Correct only the result format. Do not restart development, review or validation, read the project or call tools. Do not rewrite established work facts.""",
    },
    'publication/resume': {
        'zh': """你是负责本次交付说明的工程师。继续完成你负责的交付说明。根据当前改动和允许使用的证据，完成准确的提交说明与 PR 文案。

最后只返回文案结果 JSON，保留 result_kind、commit_message、pr_title、pr_body_markdown 和 human_blockers 字段。""",
        'en': """You are the engineer responsible for describing this delivery. Continue your assigned writing task. Complete an accurate commit message and PR copy using current changes and the evidence you are allowed to use.

Return only the publication result JSON, retaining result_kind, commit_message, pr_title, pr_body_markdown and human_blockers.""",
    },
}
