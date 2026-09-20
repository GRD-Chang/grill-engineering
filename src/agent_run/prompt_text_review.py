"""独立验收的双语对象说明、证据边界与输出语义。"""

TEXTS: dict[str, dict[str, str]] = {
    'review/read-only-validation': {
        'zh': """整个工作区保持只读；你可以启动服务、操作浏览器和创建测试数据，
构建缓存、运行数据、日志和浏览器产物等需要写入的内容使用工作区外仅服务本轮的可定位临时路径。
优先使用项目支持的外部缓存和运行目录；确实无法在只读目录运行时，允许在工作区外创建临时运行副本。
副本应对应当前工作区的实际待验收内容，包括未提交修改和未跟踪的交付文件，不能只复制 HEAD 的提交内容。
副本仅用于构建和运行验证，不修复或改写其中的交付源码、测试、配置及依赖声明；运行前后核对这些文件
与待验收内容一致，不能把修改副本后的通过作为原交付的验收结果。
结束前关闭本轮启动的服务和浏览器，清理临时副本及其他本轮临时产物。不得提交、推送、合并或修改 GitHub。""",
        'en': """Keep the entire workspace read-only. You may start services, operate browsers and create test data; place writable build caches, runtime data, logs and browser artifacts in identifiable temporary paths outside the workspace dedicated to this task.
Prefer external cache and runtime directories supported by the project. If running in a read-only directory is genuinely impossible, you may create a temporary runtime copy outside the workspace.
The copy must match the actual content under review in the current workspace, including uncommitted changes and untracked deliverables, rather than only the committed HEAD.
Use the copy only to build and run validation. Do not fix or rewrite its deliverable source, tests, configuration or dependency declarations. Verify before and after execution that these files match the content under review; a pass after modifying the copy is not acceptance evidence for the original delivery.
Before finishing, stop services and browsers started during this task and remove the temporary copy and other task-specific temporary artifacts. Do not commit, push, merge or modify GitHub.""",
    },
    'review/integration-evidence': {
        'zh': """已有子任务的原始审查、后续修改与集成证据：
{}

这些记录帮助定位风险，不替代当前整体结果的独立验收。""",
        'en': """Original review, subsequent change and integration evidence for existing subtasks:
{}

These records help locate risks; they do not replace independent acceptance review of the current integrated result.""",
    },
    'review/initial-baseline': {
        'zh': """按当前需求和代码检查本次完整范围，独立建立验收结论。""",
        'en': """Inspect the full scope of this task against current requirements and code, and establish an independent acceptance conclusion.""",
    },
    'review/attempt-budget': {
        'zh': """这是本任务的第 {0} 次独立验收，本轮结束后最多还可启动 {1} 次独立验收。
次数只用于合理安排本轮工作，不改变验收标准；不要隐瞒、降级或放行必须修复的问题。""",
        'en': """This is independent acceptance review {0} for this task; at most {1} more may be started after this round.
Use these counts only to organize this round reasonably. They do not change acceptance standards; do not conceal, downgrade or waive problems that must be fixed.""",
    },
    'review/output': {
        'zh': """本次覆盖 code-review 默认的 Markdown 报告步骤，最后只输出符合指定输出 schema 的审查结果 JSON，包含 e2e、standards、spec 三个维度。

每个 status 只取 pass、fail 或 blocked：有必须修复的问题时为 fail 且 findings 非空；
pass 与 blocked 的 findings 为空。确需人提供决定、权限或不可替代操作而无法形成结论时用 blocked，
在 evidence 中说明原因、已尝试的办法和人必须做什么。
e2e 的 evidence 说明实际操作及结果，standards 说明审查范围或基准，spec 说明验收条件及覆盖情况。
只有三个维度均 pass 才通过；不输出额外报告或问题处理对照表。""",
        'en': """This overrides code-review's default Markdown report step. Output only review JSON conforming to the supplied output schema, with e2e, standards and spec checks.

Each status must be pass, fail or blocked. Use fail with nonempty findings for problems that must be fixed. For pass and blocked, findings must be empty. Use blocked only when a human decision, permission or irreplaceable action is genuinely required before a conclusion can be reached; explain the reason, attempted approaches and necessary human action in evidence.
The e2e evidence describes actual operations and results; standards describes review scope or baseline; spec describes acceptance criteria and their coverage.
Acceptance passes only when all three dimensions pass. Do not output an additional report or an issue-disposition table.""",
    },
    'review/output-repair': {
        'zh': """你已完成本次独立验收。现在只负责根据已经完成的真实工作，重新输出符合要求的审查结果 JSON。
格式错误：{0}

只修正结果格式，不重新开发、审查、验证、读取项目或调用工具，不改写已有工作事实。""",
        'en': """You have completed this independent acceptance review. Your only responsibility now is to re-output valid review result JSON based on the actual work already completed.
Format error: {0}

Correct only the result format. Do not restart development, review or validation, read the project or call tools. Do not rewrite established work facts.""",
    },
    'review/resume': {
        'zh': """你是负责本次交付的独立验收工程师。继续完成你负责的独立验收。以当前工作区和下列准确对象为准，完成尚未结束的核验。

最后只返回本次审查结果 JSON，保留 checks 中 e2e、standards、spec 三个维度的 status、evidence 和 findings。""",
        'en': """You are the independent acceptance engineer responsible for this delivery. Continue your independent acceptance work. Use the current workspace and the exact objects below to finish outstanding validation.

Return only the current review result JSON, retaining status, evidence and findings for the e2e, standards and spec dimensions in checks.""",
    },
    'review/candidate-object': {
        'zh': """本次验收对象：
当前工作区是待验收修复与默认分支合并后的未提交预览；HEAD 留在默认分支基准是正常情况。按下面的实际默认分支基准和待合并提交检查工作树中的整体结果，不能只证明局部修复通过。""",
        'en': """Current acceptance review object:
The current workspace is an uncommitted preview of the repair under review merged with the default branch. HEAD remaining at the default-branch base is normal. Use the actual default-branch base and commit to merge below to inspect the complete result in the working tree; proving only the local repair passes is insufficient.""",
    },
    'review/commit-object': {
        'zh': """本次验收对象：
当前工作区对应待验收提交，检查比较基准到该提交的完整改动。""",
        'en': """Current acceptance review object:
The current workspace corresponds to the commit under acceptance review. Inspect the complete changes from the comparison base to that commit.""",
    },
    'review/run-object': {
        'zh': """本次验收对象：
当前工作区是默认分支与本次全部交付合并后的未提交预览。HEAD 保留在基准提交是正常情况；检查工作树中的整体结果。""",
        'en': """Current acceptance review object:
The current workspace is an uncommitted preview of the default branch merged with the entire delivery. HEAD remaining at the base commit is normal; inspect the integrated result in the working tree.""",
    },
    'review/run-repair-object': {
        'zh': """本次验收对象：
当前工作区是本次修复与默认分支合并后的未提交预览；HEAD 留在默认分支基准是正常情况，检查修复进入整体后的结果。整体开发基准与实际合并基准可能不同，不把二者混为一谈。实际合并基准从当前工作区的只读 Git 取得。""",
        'en': """Current acceptance review object:
The current workspace is an uncommitted preview of this repair merged with the default branch. HEAD remaining at the default-branch base is normal; inspect the repair as part of the integrated result. The overall development base and actual merge base may differ; do not conflate them. Obtain the actual merge base using read-only Git in the current workspace.""",
    },
    'review/identity-candidate': {
        'zh': """待合并提交""",
        'en': """Commit to merge""",
    },
    'review/identity-default-base': {
        'zh': """默认分支合并基准""",
        'en': """Default-branch merge base""",
    },
    'review/identity-expected-tree': {
        'zh': """预期合并文件树""",
        'en': """Expected merged file tree""",
    },
    'review/identity-repair-candidate': {
        'zh': """待合并的修复提交""",
        'en': """Repair commit to merge""",
    },
    'review/identity-reviewed-base': {
        'zh': """比较基准""",
        'en': """Comparison base""",
    },
    'review/identity-reviewed-candidate': {
        'zh': """待验收提交""",
        'en': """Commit under acceptance review""",
    },
    'review/identity-reviewed-tree': {
        'zh': """待验收文件树""",
        'en': """File tree under acceptance review""",
    },
    'review/identity-run-base': {
        'zh': """整体开发基准""",
        'en': """Overall development base""",
    },
    'review/identity-run-head': {
        'zh': """本次全部交付的提交""",
        'en': """Commit containing the complete delivery""",
    },
    'review/previous-result': {
        'zh': """上一次验收对象：
{0}

上一次完整审查结果（previous_acceptance_artifact 原始 JSON）：
{1}

你仍对本次完整对象负责，旧结果不能证明当前代码已经通过。""",
        'en': """Previous acceptance review object:
{0}

Previous complete review result (original previous_acceptance_artifact JSON):
{1}

You remain responsible for the entire current review object. Previous results cannot prove that current code has passed.""",
    },
}
