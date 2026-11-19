# 独立验收与证据

独立验收评估的对象、证据适用性和原始结果。角色输入及完整输出见[Prompt 合同中的独立验收](../agents/agent-prompts.md#独立验收)。

[按任务查阅术语与合同](../../GLOSSARY-MAP.md)

## 验收范围与证据适用性

**Review Context（审核上下文）**:
一次 Review 固定的唯一 `review_range`、准确需求、审核成果与适用原始证据。首次开发从开发起点、Findings 复审从紧邻实际审核提交、首次 CI 从失败 head、首次集成从目标快照开始，均到实际审核提交；新的失败 head 使用新的 CI 修复范围。完整 Parent 需求、成果和历史证据是背景，不形成第二个比较范围；预算窗口、线程切换和旧基线缺失不使范围回退到首次全量。首次审核保留规格要求的完整首次范围，Reviewer 在固定范围内自主决定核验深度，不能伪造缺失证据或擅自扩大范围。比较起点不覆盖交付基准。
_Avoid_: 仅有 from/to commit 的 diff、完整 Prompt 文本、把本轮重点变化等同于全部交付责任

**Evidence Applicability（证据适用性）**:
Review、CI 观测、Publish 文案与人工批准各自绑定实际对象；同类规则分别由 `review_evidence`、`required_checks_observation`、`publication_evidence` 与 `approval_grant` 维护。Review Context 固定需求、范围、基线与候选，结论按这些实际事实核验；完整 Parent 还比较 Ticket Graph/Completion、默认分支基线和预期合并结果。候选提升另外证明实际集成结果，不修改原候选记录以获得新授权。
CI 使用准确 PR/head 与完整检查快照；pending/unknown 不表示成功。文案复用核对实际 Publish Attempt 的输入边界和原始结果，文案单独失效时保留仍适用的候选、Review 与 CI 修复用途和证据，按既有流程更新文案；旧 Publish Attempt 不决定新候选是否失效。人工批准绑定准确 PR、head/base 和完整验收指纹；resume/message 不授权新成果。失效只改变当前适用性，status/history 保留原结论与原对象；结果未知的正式写入先按原意图读回，不作为普通缓存清除。
_Avoid_: tree 相同即复用、一套通用 passed 标志、移动历史通过标记、以恢复消息替代批准

**Delivery Quality Floor（交付质量底线）**:
自动交付在优化 Token、时间或 Reviewer 数量时不得降低的完成约束，包括 correctness、安全、数据完整性、公开契约与完整 Parent Acceptance Criteria。没有当前失败链的风格偏好、可选重构和低风险维护性意见不属于质量底线，可以延后到 Run 级集中评估或作为 Non-blocking Observation 保留。
_Avoid_: 追求零意见、以无限审查换取主观完美、把低风险建议升级为完成阻塞

**Deferred Scope Note（延期范围说明）**:
Reviewer 在当前 Ticket 审查中实际遇到、但已由明确 sibling/follow-on Issue 负责的范围边界说明。它只以 `Deferred to #N：…` 写入最相关 lane 的 `evidence`，不得进入 `findings`、改变 lane 状态或触发 Repair；Development 遇到同类边界时可写入 Development Summary。Agent 不为生成此说明而遍历完整 Ticket Set，也不负责创建、修改或维护对应 Issue；Publication 不把它叙述为当前 Ticket 的交付成果。
_Avoid_: 非阻塞 Finding、每轮枚举 sibling、由 Agent 创建 Issue、把 deferred 当作当前交付

**Non-blocking Observation（非阻塞观察）**:
Reviewer 在当前审查中实际形成、确有后续价值，但不影响当前 Ticket 接受结论的纯维护性建议、可选重构或其他意见。它只以 `Non-blocking observation：…` 写入最相关 lane 的 `evidence`，不得进入 `findings`、改变 lane 状态或触发 Repair；没有实际后续价值的轻微问题直接省略。Agent 不负责为其创建或维护 Issue，Publication 不把它叙述为当前 Ticket 的交付成果。
_Avoid_: Acceptance Finding、Human Blocker、强制寻找建议、以建议触发 Repair

**Review Boundary（审查边界）**:
一个 Change Job 中可产生必做开发工作或 blocking Finding 的唯一语义范围：Ticket Job 只包含 Ticket Contract 及其 Candidate 对新增或改变路径直接造成的工程风险；Parent-only 覆盖完整 Parent，Run Repair 与 Run Acceptance 覆盖完整 Parent、Ticket Set、累计变更和预期合并结果。Ticket Job 中的 Parent Context 与 sibling/follow-on Issue 只用于理解背景、解释当前 Ticket 明确引用且完成其 AC 所必需的约束，以及 deferred 归属；它们不能自行增加当前 Ticket 的交付项。
_Avoid_: 把完整 Parent 当作 Ticket 范围、按文件列表划定范围、把未来 Ticket 工作当作当前 Finding

**最小充分改动（Minimum Sufficient Change）**:
能够完整满足当前 Ticket 验收条件、处理本次改动直接造成的工程风险，同时不增加无关行为、状态、依赖、配置、公开入口或抽象层的最小连贯改动。它优先沿用直接适用的现有模块和约定；只有当前正确性、可测试性、已经存在的具体重复或仓库既有设计确有需要时，才进行必要的局部重构。最小不等于机械追求代码行数最少，模块化也不等于增加文件、转发层或为未来需求预留通用框架。
_Avoid_: 最少代码行、顺手重构、未来扩展点、假想复用、为拆分而拆分

**验收 Lane 状态（Acceptance Lane Status）**:
每个 E2E、Standards、Spec lane 独立输出 `status`、`evidence` 与 `findings`。`pass` 表示该 lane 已完整执行且 Findings 为空；`fail` 表示其 Findings 非空；`blocked` 表示该 lane 因权限、凭据、产品决策或不可替代外部操作而无法形成结论，Findings 必须为空且原因写入 evidence。Controller 只从三个 lane 推导结果：任一 `fail` 回到 Development；没有 `fail` 而存在 `blocked` 时成为 Human Blocker；三个均 `pass` 才通过验收。
_Avoid_: 顶层 verdict、pass 携带 Finding、fail 没有 Finding、blocked 同时产出部分 Finding

## 验收记录与修复

**验收 Finding（Acceptance Finding）**:
独立验收 Agent 在其负责的验收 lane 中发现的、必须在当前 Change Job 中处理的问题。每条 Finding 是自然说明问题、证据、所需修复和复验方式的非空字符串，不按固定措辞校验；它表达一个必须处理的问题，任一 Finding 都使所属 lane `fail`。只有同时处于当前 Review Boundary 内、有实际复现或明确代码因果证据且可定位、违反明确当前需求或硬性工程合同或造成具体风险、保持现状会使当前验收对象不可接受、且能由当前 Job 修复的问题才构成 Finding。明确需求或硬性合同的真实缺陷不会因为修复量小而降级；同一根因的多个表现合并成最合适 lane 中的一条，并说明直接影响的同族场景。E2E、Standards、Spec 三个 lane 各自保存 Findings，Controller 不设顶层 Finding 汇总或 Agent 输出的 verdict：任一 lane 的 Finding 非空即将其原样交回 Development。Reviewer 应一次报告当前审查中已经可证明的全部 Findings，不得故意逐轮滴漏；这不要求为追求穷尽而扩大 Review Boundary 或进行无边界探索。纯维护性建议、可选重构、文件大小偏好和其他非阻塞观察不得进入 `findings`，确有后续价值时可按 Non-blocking Observation 写入相关 lane 的 `evidence`，没有实际后续价值的轻微问题直接省略；需要产品决定、权限、凭据或不可替代外部操作时进入 `blocked` evidence，而非 Finding。
_Avoid_: 非空 Finding 的 pass、无行动依据的泛泛建议、Controller 解释或重写 Finding、重复写入多个 lane、故意逐轮滴漏

**Predecessor Defect Repair（前序缺陷修复）**:
Development Codex 在交付当前 Ticket 时实际发现前序已集成代码存在问题，可以直接在当前受管开发工作区中一并修复、验证并纳入当前 Candidate，不等待另开 Ticket、人工分派或前序 Job 恢复。Development Prompt 明确授予该判断与修复责任；这不要求 Agent 主动遍历历史寻找问题，也不授权新增与当前 Parent 无关的产品能力。修复内容进入当前 Ticket 的累计 diff、直接风险审查和最终 Run Acceptance。
_Avoid_: 发现问题后机械停工、为同一修复恢复已完成 Ticket、主动审计全部历史、借机扩大产品范围

**Fresh Acceptance（独立验收）**:
Candidate Commit 创建后、生成 Publication Artifact 与 Publication Commit 前，由 Controller 按 Change Job Contract 启动全新 YOLO Reviewer Thread，在独立 Validation Checkout 中针对准确的 base SHA、Candidate Commit SHA、有效 Revision、需求源和验收标准执行实际使用与代码审查。Reviewer Prompt 要求调用 `code-review` skill，对 E2E、Standards、Spec 三个维度的最终判断负责，并要求所有审查或评价型 subagent 以 `fork_turns: "none"` 接收中立事实。正常新一轮验收使用新的 Thread 和 checkout，不继承 Development Thread、旧 Reviewer Thread 或任何开发者自我判断；首次 Reviewer 按固定的首次范围完成应有审核，后续 Reviewer 额外获得紧邻上一轮 Acceptance Artifact 及其 reviewed base/Candidate identity，以核销原 Findings、当前 repair delta 和直接回归为主，按实际影响自行决定每条 lane 的复验强度；缺少具体风险依据时避免对未变化代码重复完整扫描，在固定 review_range 内自主选择验证深度，并按统一 acceptance-blocking 门槛报告 Finding。同一 Reviewer 因 `execution_failed` 或 Human Blocker 继续当前对象时复用对应 Reviewer Thread，但仍重新创建一次性 Validation Checkout 并重新核验；模型只接收当前 Reviewer 角色的短 Prompt、准确验收对象、简短审查次数、适用的当前失败或 blocker 证据及维护者最新回复。它可为验证构建和运行测试，但不得修复源码、测试、配置或 `.gitignore`，发现的问题必须进入 Acceptance Artifact。其 pass 是 Ticket Integration Gate 的正常模式；预算耗尽后的 Deterministic Ticket Fallback 是显式、非语义验收的另一模式，不能复用旧 Acceptance Record。
_Avoid_: 开发者自测、第二次 GitHub Codex Review、仅测试通过、把确定性兜底记为 Fresh Acceptance pass

**Validation Checkout（验收工作区）**:
从准确被验收 commit 或预期合并结果创建、只供一次 Fresh Acceptance 或 Run Acceptance 使用的独立临时工作区。Reviewer 以 YOLO 运行，工作区内任何修改和中间产物都不会返回 Development checkout 或 Publisher，并在验收后整体删除。
_Avoid_: Development checkout、持久 Reviewer worktree、验收后清理单个产物

**Acceptance Artifact（验收产物）**:
Fresh Acceptance 或 Run Acceptance Codex 输出的共享结构化语义判断，第一读者是下一轮 Development Codex，而非人类报告。它只包含固定的 E2E/Standards/Spec lane；每条 lane 均有 `status`、可复核 `evidence` 与自包含 `findings`。Controller 从这三条 lane 推导通过、返工或人工阻塞；Ticket Acceptance Criteria 由 Spec lane 覆盖，scope、reviewed base/head 与有效 Revision 由 Controller 绑定在外层 Acceptance Record。
_Avoid_: Acceptance Record、模糊审查摘要、Controller 生成的修复方案

**Run Acceptance Artifact（运行验收产物）**:
由 Controller 以 `acceptance_scope=run` 保存的 Acceptance Artifact。外层 Acceptance Record 将它绑定到 Parent Spec Revision、Run Feedback Revisions、Ticket Graph Revision、默认分支 reviewed base SHA、Run Branch reviewed head SHA、预期合并结果指纹与完整 Ticket Set；其 findings 原样驱动 Run Repair Job。
_Avoid_: 新的独立 Schema、Ticket Acceptance Artifact、Run PR 评论

**Human Blocker（人工阻塞）**:
顶层 Codex 判断必须由人提供产品决策、外部权限、敏感凭据或不可替代外部操作才能继续时的最小结构化请求。Fresh/Run Acceptance 以一个或多个 `blocked` lane 的 evidence 表达；Development 使用 `result_kind: "human_blocker"`、`summary: null` 和非空 `human_blockers`，Publication 使用对应五字段 flat wire contract。Controller 只保存、展示与在 resume 时原样传回 blocker；维护者可不附加响应直接授权 Agent 重新核验，也可提供仅绑定当前 Job Generation 的不可变 Human Response，后者按顺序进入后续 Development 与 Fresh Acceptance，不修改 Issue、不触发 Requeue，也不与 Run Feedback Revision 混用。每个 response 最多 8 KiB；同一 Generation 的序列不按容量截断，替换 Generation 会从空序列开始，绝不向新 Generation 注入旧响应。Ticket Job 的 Human Blocker 是局部门禁，只阻塞自身及依赖任务；独立 Ticket 继续推进。Parent-only、Run Acceptance 与 Run Publication 的 Human Blocker 仍建立 Run-wide Operator Gate。恢复成功后对应 blocker 告警清除，原始 blocker/response 尝试按顺序保留。
_Avoid_: Controller 诊断、subagent 事件、自动重试策略、笼统失败摘要

**Human Response（人工响应）**:
维护者通过 `agent-run resume <parent-issue> --message "..."` 为当前 Human Blocker 提供的原样、不可变
上下文。Controller 只做 trim 后非空和最多 8 KiB 两项机械输入边界，不判断内容正确性、不检测敏感信息，也不改写、过滤或摘要；通过边界后的文本原样按顺序绑定当前 Job Generation，进入后续 Development 与 Fresh Acceptance。它不适用于 `execution_failed`、不修改 Issue、不触发 Requeue，也不等同于 Run Feedback Revision。替换 Generation 从空序列开始。
_Avoid_: Run Feedback Revision、Issue 编辑、跨 generation 上下文

**Review Finding（审查发现）**:
Acceptance Artifact 的一个 lane 中可由 Development Codex 独立修复和验证的问题单元。它以非空字符串说明问题、证据、所需修复和复验方式，不要求固定格式；所有 Finding 都要求修复，不存在建议型或非阻塞 Finding。人工产品决策、外部权限或不可替代操作进入该 lane 的 `blocked` evidence，不伪装成 Finding。
_Avoid_: 风格意见、无证据猜测、实现方案命令

**Acceptance Repair Loop（验收修复循环）**:
Fresh Acceptance 要求修改时，Controller 将原始 Acceptance Artifact 作为新的 Development Brief 输入，在同一 Change Job 与工作分支开启新的 Development Attempt。开发线程由冻结的 [Development Thread Policy](worker.md#开发工作与产物) 选择。修复后必须重新自测、通过 Git 完整性检查并创建新 Candidate；Review 预算尚未耗尽时，由新的 Fresh Acceptance Reviewer Thread 验收。Ticket 的 Reviewer N 仍失败且仍有普通 Development 名额时，下一次 Development 处理其 Findings 后改走 Deterministic Ticket Fallback；若 `N+1` 次 Development 已因其他失败提前耗尽，则不得发布未修复 Candidate，直接以 `modification_budget_exhausted` 等待人工 `resume`。Run 的 Reviewer N+1 仍失败时进入 Review Budget Checkpoint；这里的 `N` 均来自对应层级的有效 Run Policy Snapshot。Reviewer 本身不修改代码。
_Avoid_: Reviewer 直接修复、创建新 Ticket Job、复用旧验收结论

### Acceptance Record（验收记录）

Development–Acceptance Engine 在独立验收后本地持久化的权威记录，将唯一一份 Acceptance Artifact 绑定到 acceptance scope、reviewed base、已验收 Candidate 或 Run head、对应 tree 或预期合并结果、有效 Revision 和 Reviewer 身份。Controller 从 Artifact 的三个 lane 推导通过、返工或人工阻塞；正常验收路径只有仍然 current 的三个 lane 全部 `pass` 可以授权 Publication。Ticket Review 预算耗尽后的 Fallback Publication Receipt 是只授权创建或更新 PR 的显式例外，不构成 Acceptance Record，也不授权合并。后续 Attempt 替换当前 Record，历史只按恢复需要有界保留，GitHub 只接收简洁的 Agent Run Status 投影。
_Avoid_: Publication Metadata、PR 语义正文、永久适用于整张 PR 的结论

## 审查度量

**Review Optimization Policy（审查优化策略）**:
降低 Token 与审查重复时遵循的变更优先级：能通过更准确的角色、范围和完成条件解决的问题优先只调整 Prompt；Prompt 无法机械保证的预算、currentness、结构化证据与路由才在现有 Controller 和 Artifact seam 上小幅补足；Development–Acceptance Engine、Candidate/PR/Required Checks/Run Branch 阶段、Publisher Mutation Authority 与最终 Run Acceptance 权威保持不变。Ticket、Run 与 Parent-only 的每次 Reviewer Invocation 都按 Prompt 调用 `code-review` skill，并对 E2E、Standards、Spec 三个维度的最终判断负责；所有审查或评价型 subagent 使用 `fork_turns: "none"` 和中立任务事实。首次 Reviewer 按固定的首次范围完成应有审核，预算窗口不重置比较范围；Reviewer 2+ 获得紧邻上一轮 Acceptance Artifact 及其准确 review identity，以核销原 Findings、审查从上一 Candidate 到当前 Candidate 的 repair delta 与直接回归为主，并按实际影响自行判断三条验收 lane 需要多少新验证；缺少具体风险依据时避免对未变化代码重复完整扫描，在固定 review_range 内自主选择验证深度。Reviewer 看到当前与剩余自动验收次数，定向 Repair 看到已完成与剩余次数；该投影不暴露预算窗口或改变门禁。Controller 不解析 Findings、不判断哪些已关闭、不生成 Finding Ledger，也不要求 Development 为每个 Finding 提交 disposition。每次 Reviewer 仍对准确当前 Candidate 或预期合并结果负责，并只交付新的 Acceptance Artifact；旧结论不能直接授权新 tree。
_Avoid_: 为成本优化复制流水线、用 Prompt 假装实现硬门禁、无必要重写成熟状态机、破坏 currentness 或发布权威

**Offline Session Measurement（离线会话度量）**:
从持久 Run invocation history 与 Codex session/JSONL 的真实事件只读重建时间、Token、工具调用、subagent 编排、命令执行和正式 Acceptance 实例等可观察事实的评估方式。它是与 `agent-run` 推进完全分离的按需统计工具，缺失、失败或结果未知都不得改变 Run 状态、阻塞 Run Executor 或影响任何交付权限；它不使用 Agent 自报统计，不把启发式或人工语义判断伪装成机器事实，也不从 Token counter 推导实际账单。
_Avoid_: Agent Run gate、自动后台统计、Development Summary 计数、模型估算、未标注的启发式 Review wave、自动 Finding lineage、美元成本推算

**Review Optimization Observation Campaign（审查优化观测批次）**:
Review Optimization Policy 生效后，预先选定连续 3–5 个 Ticket，消费 Offline Session Measurement 的机械事实并结合明确的数据缺失、配置漂移和外部等待说明形成方向性评估。它与统计器及 Agent Run 完成门禁分离，不把小样本历史参照声明为严格 A/B、固定比例改进或因果证明。
_Avoid_: Session Metrics、自动成功判定、选择性样本、Completion Gate、严格 A/B
