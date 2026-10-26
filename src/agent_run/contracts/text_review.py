"""独立验收的双语对象说明、证据边界与输出语义。"""


TEXTS: dict[str, dict[str, str]] = {
    'review/fixed-range': {
        'zh': """本次固定审核快照的需求与范围事实（原始证据在下方用途段落给出，均取自同一快照）：
{0}

review_range 是本次唯一固定比较范围，从 base_sha 到 candidate_sha；当前工作区的 HEAD 对应其结束提交。围绕该范围、原问题和直接回归独立验证，不以完整需求、历史基线或 Parent 背景扩大本轮审查范围，也不因旧验证缺失退回首次全量。首次审核按已固定范围完成应有的完整核验；后续使用适用原始证据形成当前结论，缺失或无法确认的证据如实说明，不能冒充通过。facts 中的开发说明只解释实际修改，不能替代独立证据。Ticket 的 parent 只提供背景。""",
        'en': """Requirements and ranges from the fixed review snapshot (original evidence appears in the purpose-specific sections below, from the same snapshot):
{0}

review_range is the only fixed comparison for this review, from base_sha to candidate_sha; the checkout HEAD matches its ending commit. Independently validate that range, the original problems and direct regressions. Complete requirements, historical baselines and Parent background do not expand this review's scope, and missing prior validation does not restart an initial full review. Complete the necessary initial validation within the fixed first-review range. Later conclusions use applicable original evidence; state missing or unverified evidence honestly rather than claiming a pass. Development explanations in facts describe actual changes but do not replace independent evidence. A Ticket's parent is background only.""",
    },
    'review/run-ci-object': {
        'zh': """本次验收对象：
当前工作区是 CI 修复与默认分支组合后的固定已提交成果，HEAD 必须对应 reviewed_candidate_sha。只使用 review_range 规定的本轮比较范围；原 CI 失败版本和完整 Parent 成果提供背景，不另行生成第二个比较范围。""",
        'en': """Current acceptance review object:
The checkout is the fixed committed result combining the CI repair with the default branch. HEAD must match reviewed_candidate_sha. Use only the current comparison defined by review_range; the original CI failure and complete Parent result provide background, not another comparison range.""",
    },
    'review/ci-object': {
        'zh': """当前工作区包含固定已提交的 CI 修复成果，HEAD 对应 reviewed_candidate_sha。审查范围只取 review_range；首次从失败 head 开始，Findings 后从紧邻实际审核提交开始，原开发基线和 CI 证据只提供适用背景。""",
        'en': """The checkout contains the fixed committed CI repair result, with HEAD at reviewed_candidate_sha. Use only review_range: the failed head for the first repair review, and the immediately previous actual review commit after Findings. The development base and original CI evidence provide applicable background.""",
    },
    'review/ci-initial-baseline': {
        'zh': """本次 CI 修复尚无上轮 CI Review 结果。依据准确失败证据、当前修复差异及直接回归，独立建立本次 CI 修复的审查结论。""",
        'en': """There is no previous CI Review result for this repair. Establish an independent conclusion for this CI repair using the exact failure evidence, current repair delta and direct regressions.""",
    },
    'review/ci-repair': {
        'zh': """你负责当前 Ticket 的 CI 修复审核。本轮完整审查和验证范围是这项 CI 修复及其直接回归；按实际风险选测，无需固定运行完整测试，发现具体风险时扩大验证。原需求用于约束本次修复；围绕准确失败版本、失败检查及可取得的原始日志，独立核验根因处理、修复证据和直接影响的回归，不重新审核整套需求。不得通过削弱有效测试、断言或检查伪造通过；开发自述和旧版本检查不能证明当前成果通过。已有 Findings、人工回复或新会话仍属于这项 CI 修复。

CI 失败（完整原始证据）：
{}""",
        'en': """You are responsible for reviewing the current Ticket's CI repair. The complete review and validation scope of this round is this CI repair and its direct regressions. Select tests based on actual risk; a full suite is not mandatory each round, but expand validation when a concrete risk requires it. Original requirements constrain this repair. Independently verify root-cause handling, repair evidence and directly affected regressions against the exact failed revision, failed checks and available original logs; do not review the full requirements again. Do not weaken valid tests, assertions or checks to manufacture a pass. Developer claims and checks on an older revision do not prove the current result passed. Findings, human replies and new sessions remain part of this CI repair.

CI failure (complete original evidence):
{}""",
    },
    'review/parent-ci-baseline': {
        'zh': """已有完整 Parent 验收基线（准确对象及原始结果）：
{}
基线用于复用旧验证证据；独立核验本次变化并对当前整体成果形成结论，旧结果不能直接授权新成果。""",
        'en': """Existing complete Parent acceptance baseline (exact identities and original result):
{}
Reuse prior validation evidence from this baseline; independently verify current changes and conclude on the entire current result. The old result cannot authorize a new result directly.""",
    },
    'review/parent-ci-repair': {
        'zh': """你负责当前 Parent 成果的 CI 修复审核。按唯一 review_range，结合准确失败检查、原始日志和直接回归定向核验；原需求约束本次修复，不重新开发或重新审核全部已完成 Ticket。结合适用的完整 Parent 证据形成当前整体成果结论，不以局部修复通过代替整体完成；证据不适用或缺失时如实说明，不将本次范围改为首次全量。不得削弱有效检查伪造通过。Review Findings、人工回复和新会话仍保留原始 CI 证据。

CI 失败（完整原始证据）：
{}""",
        'en': """You are responsible for CI repair review of the current Parent result. Follow only review_range, using the exact failed checks, original logs and direct regressions. Original requirements constrain the repair; do not redevelop or re-review every completed Ticket. Use applicable complete Parent evidence to conclude on the current overall result; a passing local repair does not prove overall completion. State missing or inapplicable evidence honestly without changing this range into an initial full review. Do not weaken valid checks to manufacture a pass. Findings, human replies and new sessions retain the original CI evidence.

CI failure (complete original evidence):
{}""",
    },
    'review/parent-integration': {
        'zh': """你负责当前 Parent 成果的集成验收。下面是准确源版本、目标版本、本地候选、最终组合及冲突路径。按唯一 review_range 核验本次组合、原冲突处理和直接回归；结合适用整体证据形成当前成果结论，不扩大为另一套完整比较。相同 tree 不能代替范围、需求版本、基线和成果身份核验。
准确集成事实：
{0}""",
        'en': """You are responsible for integration acceptance of the current Parent result. The following records exact source, target, local candidate, final combination and conflict paths. Use only review_range to verify this combination, the original conflict resolution and direct regressions. Use applicable overall evidence to conclude on the current result without adding another full comparison. An identical tree does not replace scope, requirement revision, baseline and result identity checks.
Exact integration facts:
{0}""",
    },
    'review/read-only-validation': {
        'zh': """整个工作区保持只读；你可以启动服务、操作浏览器和创建测试数据，
构建缓存、运行数据、日志和浏览器产物等需要写入的内容使用工作区外仅服务本轮的可定位临时路径。
优先使用项目支持的外部缓存和运行目录；确实无法在只读目录运行时，允许在工作区外创建临时运行副本。
副本应对应当前工作区的实际待验收内容，包括未提交修改和未跟踪的交付文件，不能只复制 HEAD 的提交内容。
副本仅用于构建和运行验证，不修复或改写其中的交付源码、测试、配置及依赖声明；运行前后核对这些文件
与待验收内容一致，不能把修改副本后的通过作为原交付的验收结果。
结束前只通过本轮持有的正常关闭接口关闭服务和浏览器，清理明确属于本轮的临时路径。
禁止 kill、pkill、killall、按 PID 清理或向自身、父进程及所属进程组发送信号；Worker 由程序回收。
不得提交、推送、合并或修改 GitHub。""",
        'en': """Keep the entire workspace read-only. You may start services, operate browsers and create test data; place writable build caches, runtime data, logs and browser artifacts in identifiable temporary paths outside the workspace dedicated to this task.
Prefer external cache and runtime directories supported by the project. If running in a read-only directory is genuinely impossible, you may create a temporary runtime copy outside the workspace.
The copy must match the actual content under review in the current workspace, including uncommitted changes and untracked deliverables, rather than only the committed HEAD.
Use the copy only to build and run validation. Do not fix or rewrite its deliverable source, tests, configuration or dependency declarations. Verify before and after execution that these files match the content under review; a pass after modifying the copy is not acceptance evidence for the original delivery.
Before finishing, use owned normal shutdown interfaces for services and browsers and remove only known task-specific temporary paths.
Do not use kill, pkill or killall, clean up by PID, or signal yourself, parent processes or your process group; the program reclaims Workers.
Do not commit, push, merge or modify GitHub.""",
    },
    'review/integration-evidence': {
        'zh': """已有子任务的原始审查、后续修改与集成证据：
{}

这些记录帮助定位风险，不替代当前整体结果的独立验收。""",
        'en': """Original review, subsequent change and integration evidence for existing subtasks:
{}

These records help locate risks; they do not replace independent acceptance review of the current integrated result.""",
    },
    'review/run-complete-scope': {
        'zh': """完整 Parent 需求、Ticket 集合与完成事实用于判断当前整体成果，Ticket 的跳过记录不是 Parent 验收豁免。首次按固定的首次范围完成整体验收；后续仅审核 review_range 中本次变化、原问题和直接回归，结合仍适用的完整证据形成结论。背景不扩大本轮范围，证据不足不能冒充整体通过。""",
        'en': """Use the full Parent requirements, Ticket set and completion facts to judge the current overall result; skipped Ticket reviews do not exempt Parent acceptance. Complete the initial overall review within its fixed initial range. Later review only the current changes, original problems and direct regressions in review_range, using still-applicable complete evidence to form a conclusion. Background does not expand this round's scope, and insufficient evidence cannot prove overall acceptance.""",
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
只有三个维度均 pass 才通过；不输出额外报告或问题处理对照表。
必须以最终结构化响应返回，返回后立即结束，不得继续调用工具；commentary 中的 JSON 不能代替最终返回。""",
        'en': """This overrides code-review's default Markdown report step. Output only review JSON conforming to the supplied output schema, with e2e, standards and spec checks.

Each status must be pass, fail or blocked. Use fail with nonempty findings for problems that must be fixed. For pass and blocked, findings must be empty. Use blocked only when a human decision, permission or irreplaceable action is genuinely required before a conclusion can be reached; explain the reason, attempted approaches and necessary human action in evidence.
The e2e evidence describes actual operations and results; standards describes review scope or baseline; spec describes acceptance criteria and their coverage.
Acceptance passes only when all three dimensions pass. Do not output an additional report or an issue-disposition table.
Return the final structured response and end immediately; do not call tools afterwards. JSON in commentary cannot replace the final response.""",
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

保留 Spec、Standards、E2E 的独立审查责任和本轮固定范围。清理仅限本轮已知临时路径；禁止 kill、pkill、killall、按 PID 清理或向自身、父进程及所属进程组发送信号。
以最终结构化响应返回本次审查结果 JSON，保留 checks 中 e2e、standards、spec 三个维度的 status、evidence 和 findings；返回后立即结束，不得继续调用工具，commentary JSON 不能代替最终返回。""",
        'en': """You are the independent acceptance engineer responsible for this delivery. Continue your independent acceptance work. Use the current workspace and the exact objects below to finish outstanding validation.

Keep the independent Spec, Standards and E2E duties and this review's fixed range. Clean only known task-specific temporary paths; do not use kill, pkill or killall, clean up by PID, or signal yourself, parent processes or your process group.
Return the current review result JSON as the final structured response, retaining status, evidence and findings for e2e, standards and spec in checks. End immediately; do not call tools afterwards. JSON in commentary cannot replace the final response.""",
    },
    'review/candidate-object': {
        'zh': """本次验收对象：
当前工作区是待验收修复与默认分支合并后的未提交预览；HEAD 留在默认分支基准是正常情况。按下面的实际默认分支基准和待合并提交检查工作树中的整体结果，不能只证明局部修复通过。""",
        'en': """Current acceptance review object:
The current workspace is an uncommitted preview of the repair under review merged with the default branch. HEAD remaining at the default-branch base is normal. Use the actual default-branch base and commit to merge below to inspect the complete result in the working tree; proving only the local repair passes is insufficient.""",
    },
    'review/commit-object': {
        'zh': """本次验收对象：
当前工作区的 HEAD 对应固定待验收提交。只按 review_range 核验本轮比较；交付身份基准用于背景，不是另一个审核起点。""",
        'en': """Current acceptance review object:
The checkout HEAD is the fixed commit under review. Inspect only the comparison in review_range; delivery identity bases provide background, not another review starting point.""",
    },
    'review/run-object': {
        'zh': """本次验收对象：
当前工作区是默认分支与本次全部交付组合后的固定已提交成果，HEAD 必须对应 reviewed_candidate_sha。按唯一 review_range 核验实际成果；完整需求和完成事实是上下文，不另行改变本轮比较范围。""",
        'en': """Current acceptance review object:
The checkout is the fixed committed result combining the default branch with the entire delivery. HEAD must match reviewed_candidate_sha. Verify the actual result using only review_range; complete requirements and completion facts provide context without changing this round's comparison.""",
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
        'zh': """交付身份基准（背景，不是本轮比较起点）""",
        'en': """Delivery identity base (background, not this review's comparison start)""",
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
