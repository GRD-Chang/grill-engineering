"""共享需求、动态事实标签与独立探针的双语静态文案。"""

TEXTS: dict[str, dict[str, str]] = {
    'context/checkout': {
        'zh': """当前 checkout：{0}""",
        'en': """Current checkout: {0}""",
    },
    'context/full-requirement-url': {
        'zh': """本次负责的完整需求：{0}""",
        'en': """Full requirements assigned for this task: {0}""",
    },
    'context/task-url': {
        'zh': """本次负责的具体任务：{0}""",
        'en': """Specific task assigned for this work: {0}""",
    },
    'context/parent-url': {
        'zh': """用于理解整体需求的背景：{0}""",
        'en': """Background for understanding the overall requirements: {0}""",
    },
    'context/human-response': {
        'zh': """当前求助与维护者最新回复（原文）：
{}

回复不等于问题已经解决。重新核对相关权威来源和受影响工作，解决后继续当前任务；仍需人处理时，按本角色输出格式说明最新情况。""",
        'en': """Current request for help and the maintainer's latest response (verbatim):
{}

A response does not mean the problem is resolved. Recheck the relevant authoritative sources and affected work, resolve the problem and continue this task. If human action is still needed, describe the current situation in this role's required output format.""",
    },
    'context/requirements-read': {
        'zh': """开始前先读取具体任务，再读取背景；只有完整需求链接时读取该需求。
使用继承环境中的只读 `gh issue view`，按链接中的 Issue 编号读取标题、正文和验收条件；保留继承的 PATH 与认证环境，不另找客户端、重新认证或改动认证配置。
Issue 评论、历史 PR 和其他人的总结只提供调查线索，不能覆盖当前需求或代替当前代码的验证。""",
        'en': """Before starting, read the specific task first and then the background. If only a full-requirements link is provided, read those requirements.
Use read-only `gh issue view` from the inherited environment to read the title, body and acceptance criteria for the Issue numbers in the links. Preserve the inherited PATH and authentication environment; do not find another client, reauthenticate or change authentication configuration.
Issue comments, historical PRs and other people's summaries provide investigation leads only. They cannot override current requirements or replace validation of current code.""",
    },
    'context/requirements-read-dependencies': {
        'zh': """读取最终子任务及依赖，取得本次整体需求。""",
        'en': """Read the finalized subtasks and their dependencies to obtain the overall requirements for this task.""",
    },
    'context/requirement-baseline': {
        'zh': """继续当前目录中已有及未提交的代码，不清空工作区或重新从零开发。

自行读取当前权威需求并建立本轮需求基线，以当前代码和下方适用的原始证据核验。程序摘要、旧开发总结、历史对话和旧验收结论不能替代权威需求与当前证据。""",
        'en': """Continue the existing and uncommitted code in this directory. Do not clear the workspace or start again from scratch.

Read the current authoritative requirements yourself and establish this round's requirement baseline. Verify against current code and applicable original evidence below. Program summaries, old development summaries, past conversations and old acceptance conclusions cannot replace authoritative requirements and current evidence.""",
    },
    'context/scope-existing-work': {
        'zh': """工作区可能已有前序任务成果；只有它们直接阻碍当前任务、破坏当前累计集成结果，或修复它们是满足当前验收条件所必需时，才进行最小必要修复。""",
        'en': """The workspace may contain results from earlier tasks. Make only the minimum necessary repairs when those results directly block this task, break the current accumulated integration result, or must be repaired to satisfy the current acceptance criteria.""",
    },
    'context/probe': {
        'zh': """你是负责结构化输出兼容性检查的工程师。本次只需在提供的空工作目录和输出格式要求下返回 status 为 ok。不调用工具、不读取项目、不访问网络或修改文件，不增加字段。""",
        'en': """You are the engineer responsible for checking structured-output compatibility. In the provided empty working directory and under the required output format, return only status set to ok. Do not call tools, read a project, access the network, modify files or add fields.""",
    },
    'context/child-task': {
        'zh': """具体任务的标题、正文和验收条件确定本次范围。背景用于理解整体目标和任务明确引用的必要约束，不自动增加其他子任务的工作。
当前修改直接造成的问题也属于本次责任。""",
        'en': """The specific task's title, body and acceptance criteria define this scope. Background explains the overall goal and necessary constraints explicitly referenced by the task; it does not automatically add work from other subtasks.
Problems directly caused by the current changes are also your responsibility.""",
    },
    'context/full-requirement': {
        'zh': """该需求的标题、正文和全部验收条件确定本次范围。
当前修改直接造成的问题也属于本次责任。""",
        'en': """The requirements' title, body and all acceptance criteria define this scope.
Problems directly caused by the current changes are also your responsibility.""",
    },
    'context/integrated-run': {
        'zh': """该需求的标题、正文和全部验收条件确定本次范围。
本次责任覆盖多个子任务的整体结果。核对累计改动、任务之间的配合和最终用户路径；不能用单个子任务或局部修复的通过代替整体完成。
当前修改直接造成的问题也属于本次责任。""",
        'en': """The requirements' title, body and all acceptance criteria define this scope.
Your responsibility covers the integrated result of multiple subtasks. Check accumulated changes, interactions between tasks and final user paths. A passing individual subtask or local repair cannot substitute for overall completion.
Problems directly caused by the current changes are also your responsibility.""",
    },
}
