# 交付对象、需求与当前性

按仓库、Parent、Ticket 和需求版本定位工作及授权。数据库关系见[状态存储](../agents/state-storage.md)，命令消歧见[Run 选择](../agents/run-selection.md)。

[按任务查阅术语与合同](../../GLOSSARY-MAP.md)

## 仓库与任务

**User Repository（用户仓库）**:
维护者自行管理的本地 Git checkout。Runner 仅用它只读识别目标远端，不修改其文件、索引、引用或 Git 元数据，也不借用其 Git object store 创建 worktree。未推送的本地内容不进入交付；获取交付后的远端代码由维护者自行完成。
_Avoid_: Runner 工作区、共享 Git 元数据、合并后自动更新用户分支

**Local Delivery Workspace（本地交付工作区）**:
Runner 在用户数据目录中为准确远端 Repository 管理的独立克隆，以及与其关联的运行状态和受管 worktree。相同本机用户从不同目录或不同用户 clone 启动同一 Repository 时定位到同一工作区；生命周期写入仅发生在 Runner 工作区和获准的远端。Managed Development Checkout 隶属该工作区，不产生新的任务身份。
_Avoid_: 用户仓库、启动目录、借用用户仓库的 worktree、跨用户或跨主机协调、磁盘扫描

### Delivery Task（交付任务）

同一用户在本机对一个 Delivery Task 同时最多拥有一个未完成 Delivery Run，不因启动目录或用户 clone 不同而重复创建；已完成或已放弃的 Run 保留为不可变历史，维护者可以显式创建一个后继 Run。不同 Repository 或同一 Repository 的不同 Parent Issue 是不同 Delivery Task，必须能够并行推进。
_Avoid_: 裸 `run_id`、单张 Child Ticket、整个 Repository、以启动目录区分任务、跨用户或跨主机唯一性承诺

### Delivery Run（交付运行）

一个 Run 覆盖 Parent Issue 及其相关 Ticket，完成依赖适用的整体交付和批准。一个 Run 拥有独立身份和集成边界；同一工作区内同一 Delivery Task 的多个 Run 只能按时间先后存在，不能并行。普通 `run` 本身就是创建或恢复授权：没有未完成 Run 且 Parent 当前可执行时创建后继 Run，旧终态 Run 只保留为历史，不复活、不覆盖。
各交付对象分别拥有自身的工作事实，Run 级故障和单对象故障有各自明确归属。当前选中对象只是观察或推进某项工作的视角，不代替其他对象的状态。
_Avoid_: 单张 Ticket、单次 Codex 执行、长期后台服务、同一 Parent 的并行 Run、复活或覆盖终态 Run、额外 `--new-run`

**Run 定位索引（Run Locator Index）**:
旧协议用于从 Run ID 关联仓库与状态位置的索引。当前仓库级状态库统一提供 Run 发现和路由；缺失、损坏或有歧义的身份按当次命令范围拒绝或显式消歧。查询只读，不回填、删除或迁移旧索引。见[状态存储](../agents/state-storage.md)与[Run 选择](../agents/run-selection.md)。
_Avoid_: 全盘搜索、历史 state 迁移、跨仓库自动 mutation、Agent 执行日志、第二套 Run state

**Local Delivery Task Index（本地交付任务索引）**:
从已提交任务与 Run 绑定读出的当前任务投影。仓库与 Parent 标识同一个 Task。尚未启动的 Task 可以为空闲，Executor 退出后仍保留当前 Run。程序按原任务身份接续，终态后由新的显式授权创建后继 Run。绑定冲突、损坏或不匹配时保留原事实并拒绝猜测。数据库字段归属见[状态存储](../agents/state-storage.md)。
_Avoid_: 第二套 Run state、完整 Run 历史、磁盘扫描、远端任务注册表、静默覆盖冲突、复活终态 Run

## 工作主体与当前性

**Ticket Contract（Ticket 合同）**:
Primary Ticket 当前 title/body 及其中 Acceptance Criteria 形成的唯一立即开发与验收合同。Ticket 对 Parent 的明确引用只引入完成本 Ticket AC 所必需的输入或行为语义，不吸收 Parent 中可独立交付的 sibling/follow-on 能力。
_Avoid_: 完整 Parent Spec、Issue 评论、sibling Ticket、未来状态机

**Parent Context（Parent 背景）**:
Ticket Job 为理解整体目标、术语和 Ticket Contract 而读取的 Parent Issue 内容。它帮助 Agent 正确解释当前 Ticket，但本身不是该 Ticket 的需求源、验收清单或新增工作授权。
_Avoid_: Ticket Contract、Parent-only Delivery 范围、Run Acceptance 范围

**Change Job Contract（变更任务契约）**:
Development–Acceptance Engine 处理 Ticket Job、Parent-only Delivery 或 Run Repair Job 时遵守的确定性事实与任务特有规则，包括需求源、有效 Revision、base/head branch、适用 PR 类型或 Run Branch 写入对象、开发与 Reviewer Prompt、修复预算以及完成规则。Controller 固定这些内容，智能 Agent 不选择或修改。
_Avoid_: Development Brief、Agent Artifact、Controller 全局配置

**Change Job Record（变更任务记录）**:
Controller 私有保存的 Work Subject、Generation、需求版本、Git 基线、Thread、Attempt、候选、PR、预算和幂等事实。Ticket、Parent-only 与完整 Parent 在各自的 subject 及准确关联关系上持久化，完整 Parent 的修复引用同一 Parent 的额度。持久 FIFO、租约和单一集成席位共同选择工作，切换焦点仍保留 Ticket Set 内的 blocked 或 completed 对象。

恢复按已提交的工作、门禁和资源事实继续。外部正式写入前重新核验身份、当前适用性和授权。缺损或协议不兼容时保留原状态、候选、工作区和成果；状态协议、已退休字段及拒绝边界见[状态存储](../agents/state-storage.md)。记录供程序恢复与校验，Worker 的任务输入按角色另行准备。
_Avoid_: Development Brief、Agent Artifact、PR 正文

### Job Generation（任务世代）

需求或明确的 base 漂移会使当前 Generation 成为
`requeue_required`；操作者执行 `requeue` 后，Controller 封存旧 generation 的轻量审计事实，
从命令执行时重新读取的权威状态创建新的 branch/PR/Thread identity，绝不迁移旧 Candidate、
Acceptance、Human Response 或 worktree。
_Avoid_: Invocation Resume、自动 rebase、跨 generation 复用 Thread

### Work Subject（工作主体）

领域工作目标包括 Ticket、Parent-only Delivery、Run Repair、Run Acceptance 和 Final Run Publication。Thread、Invocation 和分支或 PR 各自绑定该目标或其具体工作版本。多个目标可以由同一持久 subject 承载，身份与用途的归属见[状态存储](../agents/state-storage.md)。
_Avoid_: Job Generation、Agent Invocation、Codex Thread

**Currentness Boundary（当前性边界）**:
Controller 为 Work Subject 的一个 Job Generation 机械保存并比较的权威事实集合。它按阶段包含
requirements revision、适用 base/head、Candidate、Acceptance、PR、Parent、Graph、Ticket Completion
或 Run Branch 等输入；Codex Thread、Prompt、stdout 与 Agent 的语义判断不属于该边界。明确且可重建的
边界漂移会使旧 Generation 停在 `requeue_required` 或回到 fresh Run Acceptance；矛盾、缺失或未知外部
mutation 则是 Human Blocker。
_Avoid_: Agent 判断是否 stale、Thread 连续性、自动 rebase

**Execution Eligibility（执行资格）**:
一张 open Ticket 由 triage 标签表达的开始或显式恢复工作的资格；`ready-for-agent` 允许，`needs-triage`、`needs-info` 和 `ready-for-human` 阻止。资格不满足不取消既有 Ticket 工作及其成果，标签变化本身不启动或停止 Delivery Run。
_Avoid_: 启动授权、依赖已解除、完成状态

**Triage Label（分诊标签）**:
由维护者或独立分诊流程管理的 Issue 分类与领取资格标记，不属于 Runner 的写入权限。它不等同于 Delivery Run 的人工暂停状态。
_Avoid_: Runner 自动切换标签、以标签代替 Run 暂停记录

## 需求修订与依赖

**Ticket Content Revision（Ticket 内容版本）**:
一张 Ticket 当前标题和正文的稳定版本指纹。Agent Artifact 必须绑定该指纹；指纹变化会使基于旧内容产生的 Plan、代码候选和验收结论失效，但不会改变 Ticket Job 身份。若 Ticket Graph Revision 未变化，Controller 停在 `requeue_required`，只有维护者显式 `requeue` 才会从最新权威事实创建新的 Job Generation；Issue 评论可由 Codex 阅读，但不属于权威需求且不触发 Revision。
_Avoid_: Issue 评论、Git commit、Ticket Job ID

**Ticket Graph Revision（Ticket 图版本）**:
一个 Delivery Run 启动时接受的 Ticket Set 和 `blockedBy` 依赖边的稳定版本指纹。指纹变化表示固定边界失效，当前 MVP 必须 fail closed，不允许自动 Requeue、继续工作或人工吸收。
_Avoid_: Ticket Content Revision、执行顺序、运行状态

**Ticket Graph Change Summary（Ticket 图变化摘要）**:
Controller 在 Ticket Graph Revision 变化时确定性生成的集合与依赖边差异，列出新增或移除的 Ticket 和 `blockedBy` 边，帮助维护者审计 accepted/observed Graph。它只记录客观结构差异，不授权接受新图。
_Avoid_: 语义范围判断、执行顺序变化、结构确认授权

**Ticket Set（Ticket 集合）**:
Parent Spec 的 GitHub 原生 `subIssues` 所定义的 Delivery Run 工作范围。Issue 正文、标签或普通编号列表不能增加或移除其中的 Ticket。
_Avoid_: 搜索结果、ready-for-agent 标签集合、依赖邻接节点

**External Blocker（外部阻塞项）**:
被 Ticket 的 GitHub 原生 `blockedBy` 引用、但不属于当前 Ticket Set 的 Issue。它可以阻止 Ticket 激活，但不会自动成为 Delivery Run 的开发任务。
_Avoid_: 隐式新增 Ticket、正文中的阻塞描述、普通相关 Issue

**Parent Spec Revision（父规格版本）**:
一个 Delivery Run 所依据的 Parent Spec title/body 内容版本指纹。Controller 同时保留 accepted 与 observed revision 作为机械 currentness 输入；评论、assignee 与时间戳不进入 revision，具体 stale 路由由 Job Generation 规则决定。
_Avoid_: Ticket Content Revision、Ticket Graph Revision、Git commit

**Deterministic Contradiction（确定性矛盾）**:
Controller 已取得不能与当前 Change Job Record 和 Currentness Boundary 安全一致解释的权威事实，或持久状态内的 Job、语义 Attempt 与 Active Invocation 已无法满足同一规范解释时形成的 fail-closed 终态。它停止 Codex、Publisher mutation 与自动 cleanup，保留受管工作区和诊断证据；操作者可以读取状态与历史或放弃 Delivery Run，但新 Runner 不自动改写历史不一致状态，也不从普通重试、等待或 Runner 更新推断恢复授权。
_Avoid_: GitHub Convergence Wait、Supervision Timeout Pause、Human Blocker、Invocation Resume、自动恢复、历史状态迁移

**Unsupported Scope Change（不支持的范围变化）**:
运行中 observed Ticket Set 或 `blockedBy` Graph Revision 与 accepted revision 不一致时的 fail-closed 状态。Controller 保留 accepted/observed revision、变化摘要和 observed graph；该状态不运行 Codex、不新建 Thread、不 Requeue、不继续交付，也不执行 Publisher mutation。操作者只能查看状态/历史、恢复 GitHub 原图或放弃当前 Run。
_Avoid_: Scope Impact Assessment、结构确认、自动吸收、自动 Requeue
