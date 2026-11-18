# Ticket 交付与正式发布

从 Ticket 调度、候选和发布推进到远端检查、集成与关闭。正式写入的原意图与结果对账见 [ADR 0022](../adr/0022-publication-write-reconciliation.md)。

[按任务查阅术语与合同](../../GLOSSARY-MAP.md)

## Ticket 与工作队列

**Primary Ticket（主 Ticket）**:
一张 Ticket PR 唯一负责完成的 GitHub Issue，由 Ticket Job 身份确定，并通过 GitHub Development 原生关联与 PR 正文中的 `Primary Ticket` 引用共同表达。由于 PR 目标是非默认 Run Branch，Publisher 不依赖 closing keyword，而在合并后根据 Ticket Job 身份显式关闭该 Issue。
_Avoid_: Related Issue、Agent 猜测、一个 PR 关闭多张 Ticket

**Ticket Completion（Ticket 完成）**:
Ticket PR 通过 Fresh Acceptance 或 Deterministic Ticket Fallback、Required Checks 和 Published-Head Gate，并合入所属 Run Branch 后形成的完成状态。Publisher 随即显式关闭 Primary Ticket，并评论记录 Delivery Run、Ticket PR、采用的 Ticket Integration Gate 模式和进入 Run Branch 的 commit SHA，同时说明该变更尚未进入默认分支；该关闭动作使 GitHub `blockedBy` 依赖自然解除。Ticket Completion 只表示 Ticket 已进入临时集成边界，不表示完整 Parent 已最终验收。
_Avoid_: 合入默认分支、最终人工整体验收、closing keyword

**Ticket Completion Revision（Ticket 完成版本）**:
Run Acceptance 与 Run Repair Revision Snapshot 使用的确定性 Ticket 完成身份，包含 Ticket number、integrated SHA、Effective Revision、Ticket Integration Gate 模式及其绑定的 base/tree，并按 Ticket number 数值排序。完整 Acceptance Record 或 Deterministic Integration Record 继续保存在对应 Ticket Job 中，不嵌套复制到该版本身份。
_Avoid_: Ticket Completion、完整 Acceptance Record、Reviewer 文案

**Ticket Job（Ticket 任务）**:
一张 Ticket 在一个 Delivery Run 中唯一、持久且可恢复的工作身份。其内容变化、失败和普通修复只产生新的 Attempt，不产生第二个 Ticket Job、Ticket Branch 或并行 active PR；仅 post-merge、pre-completion Revision 漂移会保留已合并 PR 为 superseded integration，并为最新 Revision 顺序创建新的 active PR。Change Job Record 对每个 superseded integration 只保留旧 `pr_number`、`integrated_sha` 与 `effective_revision`，重复恢复不重复追加。
_Avoid_: Ticket、Attempt、Codex Thread

**Active Ticket Job（活跃 Ticket 任务）**:
Controller 当前选中推进的 Ticket Job，不代表 Run 中唯一正在执行或接受监督的 Ticket。一个 Run 可保存多个独立 Ticket Job，实际 Agent 工作受 Ticket Work Concurrency 约束，集成交付受单一 Ticket Integration Holder 约束。
_Avoid_: 把当前指针当成唯一 Ticket Job、让等待 CI 占开发 Worker、由 Agent 决定调度

**Ticket Work Concurrency（Ticket 工作并发额度）**:
同一 Delivery Run 允许同时调用 Agent 执行工作的 Ticket 数上限；需求开发、Review、CI 修复、冲突处理等实际 Agent 工作共用此额度，纯 CI 等待、排队与程序监督不占名额。它限制 Ticket 工作并发，不表示 Agent 内部子进程的总数上限；已确认的范围调整见 [ADR 0019](../adr/0019-ticket-work-concurrency-and-supervision.md)。
_Avoid_: 仅限制需求开发、修复另开无限额度、CI 等待占用名额、Agent 子进程总数上限

**Ticket Work Queue（Ticket 工作队列）**:
已获授权、满足执行条件且等待 Ticket Work Concurrency 名额的工作集合，包含新开发、修复和已接受的人工恢复。按就绪后的等待顺序分配空出的名额，不因工作类型自动插队，不抢占当前工作；恢复请求在无名额时持久排队，名额可用且执行条件仍满足时自动开始，无需再次 Resume。
_Avoid_: Ticket 集成队列、把人工阻塞当成排队、自动续期次数预算、Resume 自动插队、重复恢复生成重复工作

**Ticket Integration Queue（Ticket 集成队列）**:
Ticket 满足开发及原有审核条件后进入的 Run 级持久队列。规范记录为 `ticket_integration_queue` 中的 Ticket number 与 `ready_at`；先按进入待集成时间、同时时按编号排序。代码修复或异常暂停使 Ticket 释放席位并离队，修复后以新的 `ready_at` 重新排队。队列中和审核通过都不是 Ticket Completion。
_Avoid_: 沿用修复前队列位置、以 Ticket 编号替代真实就绪时间、把排队视为完成

**Ticket Integration Holder（Ticket 集成席位持有者）**:
Run Branch 同一时刻唯一获准执行 Ticket 集成及后续交付的 Ticket number，保存在 `ticket_integration_holder`。正常集成、适用审核、发布、等待 CI、合入及确认期间保留；代码修复与异常暂停释放。其他 Ticket 可继续开发和排队，但不能从持有者尚未合入的候选建立 Run Branch 基线。失联恢复先核验既有持有者和外部结果，再决定继续或释放。
_Avoid_: 第二个 Ticket 同时提交 Run Branch、等待 CI 时释放、把席位当开发 Worker

**Ticket Selection Order（Ticket 选择顺序）**:
多个 Ticket 同时具备执行资格时使用的确定性选择顺序。分配工作并发名额时优先遵循 Ticket Work Queue 的就绪等待顺序；同批新就绪 Ticket 沿用 Parent Spec 的 GitHub 原生 `subIssues` 顺序，缺少稳定顺序时按 Issue number 升序。Codex Worker 不决定该优先级。
_Avoid_: Agent 自主排序、依赖边、人工逐票选择

**Ticket Branch（Ticket 分支）**:
一个 Ticket Job 独有并在其整个生命周期中复用的开发分支。通常只对应一张持续更新的 active Ticket PR；post-merge、pre-completion Revision 漂移时分支身份保持不变，但已合并 PR 仅作为 superseded integration 历史，新 Revision 使用新的 active PR。
_Avoid_: Run Branch、Attempt Branch、临时 worktree

**Linked Branch（关联展示分支）**:
GitHub Issue 页面上可选显示的开发分支关联。它只帮助人阅读 Issue 与开发分支的关系，不证明远端 ref、PR 或 Delivery Run 的存在性；缺失或不可用不影响分支、PR、恢复或交付。
_Avoid_: 远端分支身份、PR 身份、恢复门禁

## 候选与正式写入

**Ticket 关闭归属（Ticket Close Ownership）**:
Publisher 关闭 Primary Ticket 的可重建证明：它绑定 close-intent、PR 与 integrated SHA、intent 前的 Timeline watermark、baseline 后最新的关闭事件、Publisher actor 和 Issue 的 CLOSED 状态。GitHub Issue 与 Timeline 的时间字段可能异步收敛，不能要求其字符串完全相等；证据尚未收敛时属于 Run 内部监督，只有观察到更晚外部更新、重开、actor 或 binding 不匹配时才构成 Human Blocker。
_Avoid_: 仅凭 Issue 已关闭、时间字符串相等、把最终一致性延迟当作执行失败

### Publisher（发布器）

一个 Delivery Task 的唯一 Run Executor Session 中持有写凭证的受限模块，也是系统唯一的 Mutation Authority。它只接受 Controller 已校验且被 Change Job Contract 允许的动作，执行 Git、GitHub PR、评论、Issue 与合并写入；它不是独立服务，写凭证不会进入 Codex 子进程环境。
_Avoid_: Codex Worker、内容作者、智能审查者

**Mutation Authority（变更权限）**:
改变权威 Git 历史、metadata 或 GitHub 外部持久状态的排他权限；临时 checkout 的源码编辑和一次性 Git 数据不属于该权限。该权限只属于 Publisher，不因 Codex Worker 的建议、代码修改或完成声明而转移。
_Avoid_: Agent 自治、结构化输出、候选就绪

**Merge 结果对账（Merge Outcome Reconciliation）**:
Publisher 发出带精确 head 绑定的 merge intent 后，如网络或 GitHub 响应使结果未知，先在 GitHub 只读事实中对账：已 MERGED 即恢复成功；仍 OPEN 且 head、base、Required Checks 与 mergeability 仍全部匹配时，最多重试同一 intent 三次；任一矛盾状态才停止为人工处理。结果读取使用 GitHub 事件与只读状态的监督预算。
_Avoid_: 盲目重放未知写入、仅凭 CLI stderr 判定失败、绕过 currentness 的 merge retry

**Ticket PR（Ticket 拉取请求）**:
承载一张 Ticket Job Generation 的候选变更并以所属 Run Branch 为 base 的拉取请求。Publisher 从
Primary Ticket 创建 GitHub 原生关联的 generation-local Ticket Branch，并在 PR 正文保留可读引用；
该 PR 只在自动门禁通过后使用 squash merge 进入 Run Branch，不直接进入默认分支。普通 repair 始终
更新同一 generation 的 active PR；current PR 被关闭但未合并时 Job 阻塞，不自动创建替代 PR。明确
requirements/base 漂移时旧开放 PR 被标记 superseded 并关闭，新 Generation 创建新的 branch/PR；
已合并 PR 只作为审计记录，不再编辑或复用。
_Avoid_: 最终集成 PR、多 Ticket PR、默认分支 PR

**Ticket Squash Merge（Ticket 压缩合并）**:
Publisher 在 Ticket PR 的 Published-Head Gate 通过后执行的固定合并方式。它通过正式合并请求指定 `merge_method=squash` 与准确 head `sha`，将 PR 公开分支上的 Publication 与 repair commits 作为一个新语义 commit 写入 Run Branch；首次推送前已压缩掉的本地 Candidate Commits 不会出现在 PR 页面。合并结果必须继续匹配 Ticket Integration Gate 绑定的 base、Publication tree，以及准确 merge intent 的完整提交说明。Ticket squash 使用该次 Publication 的 pr_title 与程序实际发布的完整 PR 正文，包含关联前缀；commit_message 仅用于本次发布提交。写前实际 PR 文案必须仍与冻结的 squash 说明一致，已发送意图和合并回执按原说明严格对账，不改绑新文案。
_Avoid_: Rebase merge、普通 merge commit、发布前 Candidate 压缩、仅比较提交标题

### Candidate Commit（候选提交）

Controller 以受管工作分支的准确 SHA 绑定每轮候选，Git 完整性检查、验收和修复均针对该版本。新修复向前生成候选。Ticket 和 Parent-only 的首次 PR 按发布合同压缩；完整 Parent 的修复候选先通过独立验收，再按 [ADR 0031](../adr/0031-parent-repair-pr-and-repository-ci.md) 发布 Run Repair PR。
_Avoid_: Publication Commit、远端 PR head、Agent 自行提交

**Forward Candidate Repair（前进式候选修复）**:
对已创建 Candidate 的验收或 CI Finding，Development Codex 只在同一 Change Job 的受管开发工作区修改当前文件树，可以恢复、删除或重写旧 Candidate 已引入的内容；它不得执行 `git commit`、`git reset`、`git rebase`、强推或任何 GitHub 写入。Controller 随后从该工作区创建一个新的不可变 Candidate Commit，并为其创建新的、一次性 Validation Checkout；旧 Validation Checkout 在验收结束后清理，Currentness Boundary 仍适用时保留受管开发工作区，开发线程按冻结的 [Development Thread Policy](worker.md#开发工作与产物) 选择。因而 Git 历史只前进，而最终 diff 可以比先前 Candidate 更小。
_Avoid_: 改写 Candidate 历史、为同一边界的每条 Finding 新开 worktree、复用旧 Validation Checkout、要求人工先还原文件、把可自动修复的 diff 收缩误报为 Human Blocker

**Publication Commit（发布提交）**:
Publisher 根据 Publication Artifact 保持候选 tree 不变，并使用 Agent 编写的语义 commit message 创建待发布提交。首次发布以准确 base 为父；已发布源分支的后继提交以已记录的远端发布 head 为父，Parent 集成结果同时保留必要默认分支祖先。Publisher 只用普通快进推送更新同一有效 PR；远端 head 意外变化时记录实际观测、保留本地 Candidate 并阻止覆盖。正常模式的发布权威输入是 Fresh Acceptance；Ticket 确定性兜底模式在 CI 尚未运行时由 Fallback Publication Receipt 只授权创建或更新 PR，不授权合入 Run Branch。Publisher 必须验证 Publication Commit 与对应记录绑定的 Candidate Commit tree 相同；随后 Hosted CI Gate 与 Published-Head Gate 将远端 PR head 绑定其准确 SHA，新 head 的 CI 与批准不能继承旧 SHA 结果。
_Avoid_: Candidate Commit、机械 checkpoint、远端 PR head

**Publication Artifact（发布产物）**:
Candidate Commit 取得当前路径所需的发布权威后，由独立、只读的 Publication Codex 根据当前 Issue、最终 diff 与对应证据生成的小型结构化语义产物，提供语义 commit message、`type(scope): user-facing outcome` 格式的 PR title 和符合统一 PR Narrative 的完整正文。正常模式读取完整 Fresh Acceptance 证据；Ticket 确定性兜底模式读取 Fallback Publication Receipt，并准确说明语义 Review 预算已耗尽、当前 PR 等待 Required Checks，尚未取得 Ticket 集成权威。需求、最终方案、用户影响或验证证据变化时，Publication Codex 必须重新读取当前事实并更新产物；Publisher 只校验结构并执行写入，不改写 Agent 内容。
_Avoid_: Development Summary、独立发现工作流、验证记录

**PR Narrative（PR 语义正文）**:
Publication Codex 为 Ticket PR、Parent-only PR 与 Run PR 编写并保持准确的统一耐久说明，顶部身份分别使用 `Primary Ticket: #N`、`Parent Spec: #N`、或 `Parent Spec: #N` 加 `Delivery Run: <id>`，正文默认按 `What Problem This Solves`、`Why This Change Was Made`、`User Impact` 和 `Evidence` 组织，分别说明改动前后的问题与行为、方案理由与取舍、实际用户影响及必要操作、已有验证的结果与覆盖范围；按规模调整篇幅或合并章节，不按固定标题验收。Ticket PR 还可包含 Cross-Ticket Note。`Evidence` 记录独立 Fresh Acceptance、Run Acceptance，或明确标注为非语义验收的 Deterministic Ticket Fallback 真实背景，每条以“场景 → 实际操作或命令 → 可观察结果”表达；兜底 PR 只可读总结已使用 Reviewer Artifacts、最后一次 Reviewer Artifact 之后的 Development response 与代码 delta，不得自行断言 Findings 已关闭，也不得使用 Reviewer pass、CI pass 或语义验收措辞。Required Check 结果、Candidate、SHA、门禁和生命周期等机器事实只由 Publisher 的 Agent Run Status 评论呈现，不触发 CI 通过后的 PR Narrative 重写。UI、交互或可视输出变化时才加入 before/after 图片或视频，无内容的可选段落必须整个省略。
_Avoid_: Files changed 复述、通用 checklist、Agent 内部元数据

**Cross-Ticket Note（跨 Ticket 说明）**:
Development Codex 在 Ticket PR body 的可选 `## Cross-ticket notes` 段落中记录的非权威开发上下文，每项使用 `Affects #N: ...` 明确引用目标 Issue；没有相关信息时省略整个段落。后续 Development Codex 根据 Prompt 中的 GitHub 上下文约定，使用只读 GitHub 自主查找和阅读其认为相关的 Issue、PR 与 commit；Controller 不限制、解析或路由这些说明，它们也不修改 Issue、依赖图或 Acceptance Criteria。
_Avoid_: 正式需求增量、Issue 修改、验收门禁

**Publication Metadata（发布元数据）**:
Publisher 为每张已创建的开放自动化 PR 维护的一条 `Agent Run Status` 评论，原地更新 scope、Candidate/base、Ticket Integration Gate 模式、适用时的验收 lane、Required Checks 和 next action。它只是本地权威状态的简洁远端投影，不进入 Agent 编写的 PR 语义正文，也不发布完整 Acceptance Artifact、Acceptance Record、Fallback Publication Receipt、Deterministic Integration Record、repair input 或其他嵌套 JSON。
_Avoid_: PR Narrative、Agent 自述、重复验证说明

**Publication Operation Retry（发布操作重试）**:
Controller 或 Publisher 在一次 Publication Attempt 内对 GitHub 读取收敛、带精确绑定的写入或结果对账进行的有界确定性重试。它按外部操作风险单独计数，不增加 Publication Attempt，也不被 Invocation Resume 清零或绕过。
_Avoid_: Publication Attempt、Output Repair、无界 GitHub 重试

## 集成门禁与检查

**Ticket Integration Gate（Ticket 集成就绪门禁）**:
Candidate Commit 进入 Run Branch、供下游 Ticket 使用前形成的有界证明。正常路径由 Fresh Acceptance 形成语义验收，并继续遵守目标 branch 实际配置的 Required Checks；Ticket Review 预算耗尽后，所有 Ticket 都可改用 Deterministic Ticket Fallback，由准确 PR head 按仓库实际 Required Checks 策略形成 Deterministic Integration Record：存在 Required Checks 时必须全部通过，未配置时明确记录 Required Checks Observation 的 `result=none` 并允许继续。它不是最终产品完成权威，也不重复证明完整 Parent、跨 Ticket 交互或全局低风险维护性质量，这些由 Run Acceptance 集中裁决。两种模式都必须绑定准确 base/tree/head，并在状态、Completion Revision 与最终 Run 输入中明确区分。
_Avoid_: Ticket 最终验收、把确定性兜底伪装成 Reviewer pass、缩小 Run Acceptance、隐藏门禁模式

**Deterministic Ticket Fallback（Ticket 确定性集成兜底）**:
Ticket 普通需求开发每个窗口最多 `N+1` 次 Development 与 `N` 次 Reviewer；Reviewer N 的 Findings 由最后一次 Development 处理出新 Candidate 且 Reviewer 名额确已用尽时，允许 Deterministic Ticket Fallback。该凭据保留完整 Reviewer 历史、最后修复摘要、代码差异及准确候选树，不伪造 Reviewer pass。Published-Head Gate 仍要求准确 PR head 的 Required Checks Observation 为 pass，或明确记录未配置的 none；Run Acceptance 对完整 Parent、跨 Ticket 交互及兜底成果做最终审核。可修复 CI 失败转入独立 CI Recovery Budget，不使用 fallback 额度或普通需求开发额度。
_Avoid_: 未修复 Findings 直接兜底、把未配置 CI 记为通过、伪造 Acceptance Artifact

**Fallback Publication Receipt（兜底发布凭据）**:
Ticket Review 预算耗尽后，Controller 为准确 base、Candidate SHA/tree、Effective Revision、配对的 Development/Review Budget Window、已使用的 Reviewer Invocations 及其准确 Artifact/Reviewed Candidate、最后一次 Reviewer Artifact、随后 Development Attempt 的 Summary 与代码 delta、`final_ci_fix_used` 及适用时的原 CI 失败证据与修复 delta，以及 Publication 前适用的 Git Integrity 结果形成的不可变发布凭据。Controller 只记录这些原始事实，不判断 Findings 的 closure 状态。它只授权 Publisher 沿现有 Publication 阶段创建或更新普通 Ticket PR，以便 GitHub 对准确 head 运行 Required Checks；推送、live head 与合并阶段仍分别执行当时适用的 Git Integrity Check。它不构成 Ticket Integration Gate、Acceptance Record、测试通过证据或进入 Run Branch 的权限。任何新 Candidate、base/head 或 Revision 变化都使旧凭据失效并要求生成后继凭据。
_Avoid_: Deterministic Integration Record、Reviewer pass、PR 合并权限、provisional PR 类型、本地代码验证记录

**Deterministic Integration Record（确定性集成记录）**:
Deterministic Ticket Fallback 在 Fallback Publication Receipt 绑定的准确 PR head 上完成仓库 Required Checks 策略求值后形成的 Integration Record。它不可变地绑定 base、Candidate 与 Publication SHA/tree、Effective Revision、配对的 Development/Review Budget Window、已使用 Reviewer Invocations 的状态与 reviewed Candidate、最后一次 Reviewer Artifact、随后 Development Summary 与代码 delta、`final_ci_fix_used` 及适用时的原失败 head/evidence 与修复 delta、准确 PR head，以及该 head 唯一的 Required Checks Observation。Observation 的 `result=pass` 表示配置的必需检查全部通过，`result=none` 只表示仓库没有强制 CI，不构成测试运行或通过证据。任何新 Candidate、push、base/head、Required Checks 配置或 Revision 变化都使旧记录失去集成权威。它只授权当前 Ticket tree 在 Published-Head Gate 继续成立时进入 Run Branch，是 Run Acceptance 必须读取的待整体验证证据，不是语义 Acceptance Record。
_Avoid_: Fallback Publication Receipt、Acceptance Record、Reviewer pass、把 `result=none` 记为 CI pass、可跨 head 复用的 CI 摘要、最终完成证明

**Published-Head Gate（已发布 Head 门禁）**:
Publisher 推送 Publication Commit 后，Controller 确定性验证目标 PR 的 live head、base、有效 Revision、Required Checks、mergeability 与正常路径的 Acceptance Record 或兜底路径的 Deterministic Integration Record 完全一致。Fallback Publication Receipt 只能授权推送 PR，不能单独通过本门禁。它不进行第二次 Codex 语义审查；任何 head、base 或 Revision 漂移都会使旧门禁证据失效，最终合并使用 `--match-head-commit` 绑定准确 SHA，并在完成 Ticket 前验证 integrated commit 的 parent、tree 和完整提交说明（标题与正文）。若远端已合并但本地同步中断，恢复必须先对齐本地 Run Branch，再完成 Ticket。
_Avoid_: Fresh Acceptance、GitHub PR Exact-Head Review Loop、智能代码判断

**Ticket Remote Integration（Ticket 远端集成）**:
Ticket 的 Development、暂停续接和 Fresh Acceptance 固定在当前 Job 的原始 base 与 Candidate；普通远端源分支或 Run Branch 追加不作废这些语义轮次。原 Candidate 获得 Acceptance Record 或 Fallback Publication Receipt 后，Controller 读取 live 源与目标 SHA，先机械集成双方成果，再以集成后的准确 tree 发布或更新原 PR；初始源 ref 与原 base 相同，不作为独立开发成果。集成记录同时保存原 base/Candidate/tree、远端源/目标、集成 head/tree 和冲突路径，供 Parent 验收区分原 Ticket 审查对象与后续组合。远端新快照使已发布 head 的检查和合并权限失效，但不自动恢复已暂停的 Job，也不凭机械组合伪造新的 Ticket Acceptance。集成中的真实 Git 失败与冲突以具体操作、命令、准确版本、现场及原始输出交给 Development，不按错误白名单筛选，也不增加行尾专项策略。处理后直接返回程序的 Git 检查、提交与集成，不新增 Review。集成 Development 使用独立累计额度 `ticket_integration_development_limit`（默认 3），冲突处理同样扣费；新失败、新快照不重置额度，状态重读及同语义中断续接不重复扣费。额度耗尽保留现场并暂停，显式 `resume` 仅追加此额度、保留累计用量和历史；无法继续时明确暂停。远端非追加历史改写和身份、权限门禁继续保留。首次集成免费，其后默认最多自动同步三个外部新快照，额度随有效 Run Policy 固定；自己的发布、回读和重复操作不计数。额度耗尽仅由显式 `resume` 续期该额度。实际合入前复核 live 版本与集成记录，服务端最后瞬时竞争仍以准确 head 约束为准。
_Avoid_: 把远端推进变成 Ticket Reviewer 重审、把机械集成当语义验收、自动解除人工暂停、以旧 PR head 的检查授权新 head

**Git Integrity Check（Git 完整性检查）**:
Publisher 在创建、压缩、推送和合并候选时执行的确定性 Git 校验，包括 clean tree、预期 HEAD、base 绑定、压缩前后 tree equality、远端 lease 和 live head equality。它不运行仓库测试，也不判断实现是否正确。任何失败都不创建独立 Git-fix 阶段、专用 Agent 或独立预算，而是将原始失败证据以 `repair_source=git_integrity` 交给同一 Change Job，开始一次普通 Development Attempt。线程由冻结的 [Development Thread Policy](worker.md#开发工作与产物) 选择；该 Agent 仍不得 commit、push、force-push、rebase 或 merge。Development 产出新 Candidate 后重新执行完整检查；普通 Development 预算耗尽则进入人工 Checkpoint，维护者可用 `resume` 开启新预算窗口后继续。失败检查本身不消耗 Development 或 Reviewer 预算，只有实际恢复的 Development Attempt 按普通规则计数；检查通过前不得继续 Publication、PR 更新、合并或 Ticket Completion。
_Avoid_: Hosted CI Gate、Fresh Acceptance、本地代码 Validation、独立 Git-fix 状态机、Git-fix 专用预算、授予 Development 发布权限

**Required Checks Observation（合并前检查观测）**:
仓库规定合并前必须通过的自动检查（Required Checks）在一张 Published PR 准确 head 上的一次完整观测，绑定 PR 身份、head SHA、`none|pass|pending|unknown|fail` 汇总结果与实际 Check 明细。Ticket、Parent-only 与最终 Run PR 使用同一观察语义；新 publication head 使旧观察失效，但各自的等待、Repair、Approval 与 Merge 规则保持独立。人类视图统一称为“合并前检查”，有明细时带实际检查名称；范围仅为仓库规定必须通过的自动检查，不包含人工批准、Agent 本地验收或全部合并条件，也不代表所有 CI。未配置强制检查不等于仓库没有 CI。
_Avoid_: 面向用户单独使用“必需检查”、将合并前检查等同全部 CI、把未配置强制检查称为没有 CI、仅有汇总结果、旧 head 的观察、CI Failure Evidence、为满足字段形状合成空 Check

**CI Failure Evidence（CI 失败证据）**:
Required Checks Observation 为 `fail` 后，Controller 针对同一准确 PR head 取得的实际失败检查及可取得的原始日志，作为 CI Development 的调查输入；Actions job 存在时必须匹配该 head 且已完成。三种交付对象均不要求先归类为代码可修，平台或未知类别的真实失败同样可交给 Development。pending、unknown 或未绑定准确版本的结果不能构成可靠失败输入。
_Avoid_: Required Checks Observation、pending evidence、汇总结果、未绑定 head 的失败日志

**Hosted CI Gate（托管 CI 门禁）**:
任何 Published PR 推送后，Controller 读取适用于 PR base 的 GitHub Ruleset 或 branch protection，针对准确 live head 取得 Required Checks Observation。配置的必需检查必须全部通过；`result=none` 表示未配置，不宣称 CI 已运行。Ticket 的准确真实失败进入独立累计 CI 开发额度，包括旧代码可修分类之外的失败；CI Review 默认关闭，开启时每轮审核，免审修复用绑定原发布授权和失败证据的收据更新 PR。pending、unknown 或证据尚未收敛进入有界监督；无法开展调查时明确暂停并保留继续入口。两种 Parent 强制 Review，在已有适用完整基线上增量核验并对当前整体负责；每次合并仍要求准确当前 head 的门禁通过。
_Avoid_: 绕过 Ruleset、将未配置 CI 记为通过、同一 head 重复计轮、伪造 Reviewer pass
