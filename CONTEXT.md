# Agent 交付自动化

本上下文描述以结构化 Agent 工作结果驱动、由确定性程序控制外部写入的自动交付流程。

## Language

**Codex Worker（Codex 工作器）**:
在隔离工作区内进行规划、代码编辑、验证或审查，并返回结构化 Artifact 的智能执行者。它不持有 GitHub 写凭证，也不具备外部交付状态的变更权限。
_Avoid_: GitHub Bot、Publisher、Mutation Authority

**Worker 只读凭据续签（Worker Read Credential Renewal）**:
一个最长三小时的 Codex Worker 在读取 GitHub 前获得短期、只读的 GitHub App installation token。Controller 在 token 即将失效时自动换发；换发出现短暂失败时在十分钟内有界重试。只有旧 token 已失效且重试仍失败，Worker 才以可恢复的凭据失败暂停。Worker 不获得 App 私钥或 Publisher 写凭据。
_Avoid_: 延长 installation token 的有效期、向 Worker 暴露 App 私钥、无限重试、直接中断

**Agent Artifact（Agent 产物）**:
Codex Worker 返回的结构化意图、判断与证据。它可以包含代码变更的语义说明及待发布内容，但本身不授权任何外部写入或完成状态。
_Avoid_: GitHub 状态、完成证明、自由文本交接

**验收 Finding（Acceptance Finding）**:
独立验收 Agent 在其负责的验收 lane 中发现的、必须在当前 Change Job 中处理的问题。每条 Finding 由 Agent 用一段自包含文本写明问题、可观察证据、必须达到的结果和复验方式；不另分 P1/P2 或建议类别。E2E、Standards、Spec 三个 lane 各自保存 Findings，Controller 不设顶层 Finding 汇总或 Agent 输出的 verdict：任一 lane 的 Finding 非空即将其原样交回 Development。只有纯主观偏好或与当前 Ticket 无关的未来想法不构成 Finding；不得因问题优先级较低而省略有事实依据的 Finding。
_Avoid_: 非空 Finding 的 pass、无行动依据的泛泛建议、Controller 解释或重写 Finding、重复写入多个 lane

**验收 Lane 状态（Acceptance Lane Status）**:
每个 E2E、Standards、Spec lane 独立输出 `status`、`evidence` 与 `findings`。`pass` 表示该 lane 已完整执行且 Findings 为空；`fail` 表示其 Findings 非空；`blocked` 表示该 lane 因权限、凭据、产品决策或不可替代外部操作而无法形成结论，Findings 必须为空且原因写入 evidence。Controller 只从三个 lane 推导结果：任一 `fail` 回到 Development；没有 `fail` 而存在 `blocked` 时成为 Human Blocker；三个均 `pass` 才通过验收。
_Avoid_: 顶层 verdict、pass 携带 Finding、fail 没有 Finding、blocked 同时产出部分 Finding

**Development Brief（开发简报）**:
提供给 Development Codex 的最小语义输入，包括 Parent Issue URL、适用时的当前 Ticket URL、不可重建的原始失败证据、GitHub 上下文约定和输出要求。当前 Issue 的 title/body 是需求源；Issue 评论、历史 PR、开发者总结和旧 Artifact 仅是调查线索，不能覆盖当前需求或单独构成验收证据。Codex 直接从本地 worktree 与只读 GitHub 访问获取代码、Issue 与历史 PR 事实，并自主判断需要读取哪些相关对象；Controller 不把 Delivery Run、Revision、SHA 或其他内部运行账本作为任务输入。
_Avoid_: Change Job Record、完整环境快照、实现计划

**Delivery Hygiene（交付卫生）**:
Development 与 Repair 在当前 checkout 中承担完整交付整理责任：检查全部未提交内容，保留本任务必须交付的代码、测试、文档和配置，删除本次产生的临时、构建和测试产物；仅长期、可再生且不应版本控制的项目产物可进入 `.gitignore`。Fresh Validation、Run Acceptance 与 Publication 只能清理自己创建的验证或临时产物，不得整理交付内容或修改源码、测试、配置和 `.gitignore`。任一 Codex 在 checkout 外创建的临时路径必须可定位、只服务本次任务并在完成前清理，不得进行宽泛删除。Codex 不提交，最终 Git/GitHub 写入仍属于 Publisher。
_Avoid_: 用 `.gitignore` 隐藏交付、验证者修改交付、留下不应交付的中间产物、Codex 自行提交

**Managed Development Checkout（受管开发工作区）**:
Controller 为一个 Change Job 创建并在 Development Attempt、Repair 与 Resume 间复用的专属 worktree。它与主工作区、其他 Change Job 和一次性 Validation Checkout 隔离；系统只允许该 Job 的 Codex 整理其未提交内容，并在 Candidate 创建后交由 Publisher 写入 Git 历史。系统运行约束禁止向其中人工混入无关改动。
_Avoid_: 主工作区、共享 scratch worktree、Validation Checkout

**Development Attempt（开发尝试）**:
Development Codex 在一个 Change Job 的准确 Effective Revision 上完成内部规划、代码编辑和开发验证的一轮工作。同一 Change Job 的各次 Development Attempt 复用其 Development Thread，通过新的 Turn 接收最新版 Development Brief 与 Acceptance Artifact；它直接产出 worktree diff 与 Development Summary，不经过独立 Planning Phase。
_Avoid_: Change Job、Plan Artifact、Acceptance Attempt

**Development–Acceptance Engine（开发验收引擎）**:
Controller 复用的单一自动变更循环：持久 Development Thread 修改 checkout，Publisher 创建 Candidate 与 Publication Commit，Fresh Reviewer 输出 Acceptance Artifact，可修复 findings 原样返回开发，pass 后由 Publisher 推送 PR、等待 Required Checks、执行 Published-Head Gate，并按 Job 的合并策略完成合并。Ticket Job、Parent-only Delivery 与 Run Repair Job 只通过不同 Job Contract、Prompt、上下文、批准策略和完成规则使用该引擎，不复制控制流；Parent-only 的人工批准只授予一次合并权限，批准后的重新核验、合并与恢复仍由该引擎执行。
_Avoid_: Ticket 专用流水线、Run Repair 专用流水线、动态 Agent 编排

**Change Job Contract（变更任务契约）**:
Development–Acceptance Engine 处理 Ticket Job、Parent-only Delivery 或 Run Repair Job 时遵守的确定性事实与任务特有规则，包括需求源、有效 Revision、base/head branch、目标 PR 类型、开发与 Reviewer Prompt、修复预算以及完成规则。Controller 固定这些内容，智能 Agent 不选择或修改。
_Avoid_: Development Brief、Agent Artifact、Controller 全局配置

**Development Thread（开发线程）**:
一个 Change Job 独有并跨 Development Attempt 复用的持久 Codex Thread。它保存该任务的开发与修复上下文，但每个 Turn 都必须重新提供当前权威 Issue 引用、准备好的 checkout 和适用的原始反馈证据；其身份由 Change Job Record 保存，不与任何 Reviewer Thread 共享。因 Human Blocker 或 `execution_failed` 暂停后，`resume` 默认复用原 Thread 与保留的工作区并让 Codex 重新核验；Thread 无法恢复时停止为 `execution_failed`，只能由维护者显式用 `--new-thread` 开启标准阶段 Prompt 的新 Thread。
_Avoid_: Development Attempt、Reviewer Thread、Delivery Run 全局会话

**Change Job Record（变更任务记录）**:
Controller 私有保存的 Change Job 身份、Development Thread 身份、Revision Snapshot、Git 基线、Attempt、PR、预算和幂等状态。Ticket Job、Parent-only Delivery 与 Run Repair Job 使用同一规范记录骨架和生命周期语义，各自特有数据位于明确的 job-specific 部分；Ticket Job Record 按 Ticket 身份持久保存，并与当前 `active_ticket_job` 指针分离，Controller refresh 可以切换 active，但不得删除仍属于 Parent Ticket Set 的 blocked 或 completed Job。Job-local `blocked_reason` 保存恢复授权所需的阻塞原因；顶层 diagnostics 只是可重建的当前展示，Controller refresh 会从未解除的 deterministic blocker 幂等重建 blocked 投影。Controller 在任何外部 mutation 前验证唯一规范结构；不符合时返回 `incompatible_run_state` 并要求重新创建或清理该 Run，不使用 schema 版本号、迁移器或兼容读取。该记录用于恢复与校验，不作为需要 Codex 理解或复述的任务输入。
_Avoid_: Development Brief、Agent Artifact、PR 正文

**Job Generation（任务世代）**:
同一 Work Subject 在一组准确 requirements、base/head、Candidate、Acceptance 与 PR currentness
边界下的一次可执行身份。requirements 或明确的 base 漂移会使当前 Generation 成为
`requeue_required`；操作者执行 `requeue` 后，Controller 封存旧 generation 的轻量审计事实，
从命令执行时重新读取的权威状态创建新的 branch/PR/Thread identity，绝不迁移旧 Candidate、
Acceptance、Human Response 或 worktree。
_Avoid_: Invocation Resume、自动 rebase、跨 generation 复用 Thread

**Work Subject（工作主体）**:
一个跨 Job Generation 保持稳定的交付目标：Ticket、Parent-only Delivery、Run Repair、Run
Acceptance 或 Final Run Publication。它是审计和 currentness 绑定的对象，不等于一次 Codex
Thread、一次 Invocation 或一个可变 branch/PR identity。
_Avoid_: Job Generation、Agent Invocation、Codex Thread

**Currentness Boundary（当前性边界）**:
Controller 为 Work Subject 的一个 Job Generation 机械保存并比较的权威事实集合。它按阶段包含
requirements revision、适用 base/head、Candidate、Acceptance、PR、Parent、Graph、Ticket Completion
或 Run Branch 等输入；Codex Thread、Prompt、stdout 与 Agent 的语义判断不属于该边界。明确且可重建的
边界漂移会使旧 Generation 停在 `requeue_required` 或回到 fresh Run Acceptance；矛盾、缺失或未知外部
mutation 则是 Human Blocker。
_Avoid_: Agent 判断是否 stale、Thread 连续性、自动 rebase

**Execution Guard（执行约束）**:
Codex 以 YOLO 运行，并可自由读写宿主文件系统、联网及使用完整真实 Git/`gh` CLI；Execution Guard 不提供通用 filesystem、network、审批或命令隔离。它只通过只读权威 Git metadata 和不向 Worker 注入 Publisher 写凭据保留 Mutation Authority；Worker 启动前会拒绝 local Git config 中带 userinfo 的 HTTP(S) remote URL，并只返回不含 URL 或凭据的固定错误，不改写权威 config。该边界不承诺抵抗恶意进程、主动凭据搜索、宿主污染或数据外泄。
_Avoid_: hardened security sandbox、恶意代码隔离、全局 Git 配置、Publisher 权限

**Trusted Subagent Contract（受信任 Subagent 契约）**:
Development 与 Repair Codex 必须在自测后按 Prompt 派发 Standards 与 Spec 预审 subagent，尽早修复发现的问题；该预审不构成通过决定，也不替代独立验收。Fresh Validation 与 Run Acceptance Codex 必须按 Prompt 派发独立的 E2E、Standards 与 Spec 三条 lane，处理派发失败并重新派发，且不得用父 Agent 自签替代缺失 lane。Controller 不审计 Codex 内部事件流、subagent 身份或 skill 调用 provenance；它只校验父 Reviewer Thread 新鲜性、三 lane 的 status、evidence、findings 与外层 SHA/Revision 绑定。内部 subagent 发现的人工阻塞先交给本阶段顶层 Codex，并由其写入对应 lane 的 `blocked` evidence；只有顶层 Codex 的最终结构化输出可传给 Controller。
_Avoid_: Controller 内部 Agent 编排器、subagent provenance ledger、父 Agent 自签

**Controller（控制器）**:
本地 `agent-run` 单进程中的确定性编排层。它读取 GitHub 与本地事实、维护状态机和 Revision、选择可执行 Job、启动 Codex Threads、校验 Artifacts、执行预算与门禁，并调用 Publisher 完成允许的写操作；它不替 Agent 做需求、代码或修复方案的语义判断。
_Avoid_: Codex Worker、独立 daemon、GitHub Mutation Authority

**Run 内部监督（In-Run Supervision）**:
一次由维护者显式启动或恢复的 `agent-run run`，在可自动判定的远端异步边界（例如 Required Checks、GitHub 事件最终一致性）内自行等待、退避重试和重新读取权威事实；维护者不为普通等待另行启动 watcher 或重复输入同一命令。GitHub 读取或对账的未知非零退出默认进入有界监督，Controller 只保存经脱敏、有界的错误证据，不从 `gh` stderr 推断网络、认证、权限、代理或其他具体原因。只有结构化远端事实已证明 Publisher intent、身份、head/base、检查、状态或关闭证据矛盾时，才转换为 Human Blocker；最终人工批准仍是独立授权边界。
_Avoid_: 维护者轮询 CI、常驻的第二套控制器、自动越过 Final Human Acceptance

**监督截止时间（Supervision Deadline）**:
Run 内部监督按远端状态类别采用有限等待预算。预算内保持自动轮询与重对账；到期时保存最后的权威证据并进入监督超时暂停，而不是执行失败或假装通过。GitHub 只读状态或事件收敛的默认预算为 10 分钟，Required Checks 的默认预算为 45 分钟；其他类别须有自己的明确预算。
_Avoid_: 无限占用进程、把超时吞成 pass、把正常 pending 伪装成需要外部授权的 Human Blocker

**监督超时暂停（Supervision Timeout Pause）**:
可自动判定的远端异步状态在其监督截止时间内仍未收敛时，Run 保存最后的权威证据并退出，等待维护者显式继续；它不要求维护者提供产品决策、权限、凭据或其他额外操作。后续 `agent-run run` 或 `agent-run resume` 重新读取权威状态并开始新的对应等待窗口。
_Avoid_: execution_failed、Human Blocker、常驻 watcher、无界单次进程

**Ticket 关闭归属（Ticket Close Ownership）**:
Publisher 关闭 Primary Ticket 的可重建证明：它绑定 close-intent、PR 与 integrated SHA、intent 前的 Timeline watermark、baseline 后最新的关闭事件、Publisher actor 和 Issue 的 CLOSED 状态。GitHub Issue 与 Timeline 的时间字段可能异步收敛，不能要求其字符串完全相等；证据尚未收敛时属于 Run 内部监督，只有观察到更晚外部更新、重开、actor 或 binding 不匹配时才构成 Human Blocker。
_Avoid_: 仅凭 Issue 已关闭、时间字符串相等、把最终一致性延迟当作执行失败

**Publisher（发布器）**:
`agent-run` 单进程中唯一持有写凭证的受限模块，也是系统唯一的 Mutation Authority。它只接受 Controller 已校验且被 Change Job Contract 允许的动作，执行 Git、GitHub PR、评论、Issue 与合并写入；它不是独立服务，写凭证不会进入 Codex 子进程环境。
_Avoid_: Codex Worker、内容作者、智能审查者

**Mutation Authority（变更权限）**:
改变权威 Git 历史、metadata 或 GitHub 外部持久状态的排他权限；临时 checkout 的源码编辑和一次性 Git 数据不属于该权限。该权限只属于 Publisher，不因 Codex Worker 的建议、代码修改或完成声明而转移。
_Avoid_: Agent 自治、结构化输出、候选就绪

**Delivery Run（交付运行）**:
由一次人工授权启动、覆盖一组相关 Ticket 并以最终整体验收结束的交付范围。不同 Delivery Run 彼此拥有独立身份和集成边界。
_Avoid_: 单张 Ticket、单次 Codex 执行、长期后台服务

**Run 定位索引（Run Locator Index）**:
本机维护的最小 Run ID 到仓库根和 state 目录的定位记录。它只让新版本创建的 Run 在任意目录下由 `status` 与 `history` 找到正确的本地 state，不回填或迁移历史 Run；记录失效或冲突时要求维护者显式指定 state 目录，不扫描磁盘。索引最多保留最近 32 条，且不参与 Agent 编排、GitHub 状态、权限或生命周期 mutation。
_Avoid_: 全盘搜索、历史 state 迁移、跨仓库自动 mutation、Agent 执行日志、第二套 Run state

**Merge 结果对账（Merge Outcome Reconciliation）**:
Publisher 发出带精确 head 绑定的 merge intent 后，如网络或 GitHub 响应使结果未知，先在 GitHub 只读事实中对账：已 MERGED 即恢复成功；仍 OPEN 且 head、base、Required Checks 与 mergeability 仍全部匹配时，最多重试同一 intent 三次；任一矛盾状态才停止为人工处理。结果读取使用 GitHub 事件与只读状态的监督预算。
_Avoid_: 盲目重放未知写入、仅凭 CLI stderr 判定失败、绕过 currentness 的 merge retry

**Operator Start（人工启动）**:
维护者通过本地显式命令选择 Parent Spec 并授权创建或恢复 Delivery Run。它是唯一启动授权，任何 GitHub 标签都不能替代。
_Avoid_: 标签触发、定时触发、自动 intake

**Execution Eligibility（执行资格）**:
一张 open Ticket 由 triage 标签表达的当前可执行性。`ready-for-agent` 允许激活；`needs-triage`、`needs-info` 和 `ready-for-human` 阻止激活，标签变化本身不启动 Delivery Run。
_Avoid_: 启动授权、依赖已解除、完成状态

**Run Branch（运行分支）**:
一个 Delivery Run 独有的临时集成分支。已通过自动验收的 Ticket 变更先进入该分支，整个 Delivery Run 最终通过它接受人工整体验收后才进入默认分支。
_Avoid_: 默认分支、Ticket Branch、永久集成分支

**Parent-only Delivery（仅 Parent 交付）**:
当 Parent Issue 没有任何 Child Ticket 时使用的轻量完整交付：Publisher 创建与 Parent Issue 原生关联的唯一 Parent Branch 与直达默认分支的 Parent PR。它仍遵循共享的 Candidate-first、Fresh Validation、Required Checks 和 Published-Head Gate；`agent-run approve <run-id>` 只授予一次合并权限，Development–Acceptance Engine 随后重新核对当前 Parent Revision、验收记录、检查、默认分支与 PR head，并通过 Publisher 以普通 merge 合并。已合并但 closeout 响应丢失时，共享 Engine 只重试幂等审计评论和关闭，不得再次合并。
_Avoid_: Run Branch、Final Run PR、跳过独立验收、自动合并

**Ticket PR（Ticket 拉取请求）**:
承载一张 Ticket Job Generation 的候选变更并以所属 Run Branch 为 base 的拉取请求。Publisher 从
Primary Ticket 创建 GitHub 原生关联的 generation-local Ticket Branch，并在 PR 正文保留可读引用；
该 PR 只在自动门禁通过后使用 squash merge 进入 Run Branch，不直接进入默认分支。普通 repair 始终
更新同一 generation 的 active PR；current PR 被关闭但未合并时 Job 阻塞，不自动创建替代 PR。明确
requirements/base 漂移时旧开放 PR 被标记 superseded 并关闭，新 Generation 创建新的 branch/PR；
已合并 PR 只作为审计记录，不再编辑或复用。
_Avoid_: 最终集成 PR、多 Ticket PR、默认分支 PR

**Run PR（运行拉取请求）**:
全部 Ticket Completion 且准确 Run Branch head 通过 Run Acceptance 后，才由 Run Branch 指向默认分支创建或刷新正文的最终集成 PR。它接受 Hosted CI，通过后交给维护者进行整个 Delivery Run 的最终人工验收；人工批准后使用普通 merge commit 和 `--match-head-commit` 合入默认分支，从而保留每张 Ticket 的 squash commit，并形成一个可整体回滚的 Run 边界。
_Avoid_: Ticket PR、squash 整个 Delivery Run、自动合入默认分支

**Run Acceptance（运行整体验收）**:
全部 Ticket Completion 后、首次创建 Run PR 前以及任何 Run Branch 或默认分支更新后执行的独立整体验收。正常 Run Acceptance Attempt 使用全新 YOLO Reviewer Thread 和独立 Validation Checkout，针对 Parent Spec、Run Feedback Revisions、完整 Ticket Set、默认分支 reviewed base SHA、准确 Run Branch head SHA、预期合并结果、累计 diff 和各 Ticket Acceptance Records 进行检查；失败或 Human Blocker 只能通过显式 `resume` 继续同一 Thread，或用 `--new-thread` 新开 Thread，且仍重新创建一次性 Validation Checkout 并重新核验边界。完整 Acceptance 输出不合法时最多进行两次同 Thread、只读 Output Repair。Run Reviewer 不复用任何 Development Thread 或 Ticket Reviewer Thread，重点检查跨 Ticket 交互、整体需求遗漏、局部实现累计偏离和集成回归；默认分支 base、Run Branch head 或有效需求 Revision 漂移都会使结论失效，只有通过后才允许生成或刷新 Run PR Narrative。
_Avoid_: Ticket Fresh Acceptance、简单汇总各票 pass、最终人工验收

**Default Branch Drift（默认分支漂移）**:
最新 Run Acceptance 之后默认分支 head 发生变化，使最终预期合并结果不再是 Reviewer 验收过的组合。Controller 自动废弃旧 Run Acceptance 与 Run PR Narrative，并针对新 base 和原 Run Branch head 启动全新整体验收；若预期合并产生冲突，则把冲突证据交给 Run Repair Codex，只有无法自动解决或耗尽预算时才请求人工介入。
_Avoid_: Ticket Content Revision、人工逐次确认、复用旧验收

**Run Publication Codex（运行发布 Codex）**:
Run Acceptance 通过后由 Controller 启动的只读 YOLO Codex，读取 Parent Spec、完整 Ticket Set、各 Ticket PR、准确累计 diff 与真实验证证据，生成符合统一 PR Narrative 的 Run PR title/body。正常 Run Publication Attempt 使用新的 Codex Thread；失败或 Human Blocker 只能通过显式 `resume` 继续同一 Thread，或用 `--new-thread` 新开 Thread，并重新读取权威状态后继续或再次报告 blocker。完整 flat contract 输出不合法时最多进行两次同 Thread、只读 Output Repair。它的职责只包含发布语义，不复用 Reviewer Thread、不执行验收、不整理 Candidate checkout；若自己创建 checkout 外临时路径，负责在完成前按 Delivery Hygiene 清理。Run Branch、默认分支 base、有效需求或证据变化后必须基于新状态重新生成。
_Avoid_: Run Acceptance Reviewer、Controller 拼接正文、Run Repair Thread

**Run Repair Thread（运行修复线程）**:
Delivery Run 独有并跨最终集成修复 Attempt 复用的持久 Development Codex Thread。它只接收 Run Acceptance Artifact、Run PR CI Evidence 或默认分支合并冲突证据，以及 Parent Spec、完整 Run diff 和当前 Run Repair checkout，不复用任何 Ticket Development Thread；无法恢复时按 Development Thread 的相同显式 Resume / `--new-thread` 规则停止或恢复。
_Avoid_: Ticket Development Thread、Run Reviewer Thread、人工修复会话

**Run Repair Job（运行修复任务）**:
一次 Run Acceptance、Run PR CI 或默认分支合并冲突修复循环使用 Change Job Contract 创建的 Development–Acceptance Engine 实例。它以当前 Run Branch 为 base、原始失败 Artifact、CI Evidence 或冲突证据为修复输入，并拥有一张稳定 Run Repair PR；同一时刻最多一个 Run Repair Job 活跃，前一 Job 合并后若新的验收、CI 或合并预检再发现问题则创建新 Job，但继续复用 Run Repair Thread。
_Avoid_: Ticket Job、整个 Delivery Run 唯一 PR、独立修复流水线

**Run Repair PR（运行修复拉取请求）**:
一个 Run Repair Job 独有并在该 Job 的多轮 Development Attempt 中稳定复用的 PR，其 head 是独立 run-repair branch，base 是 Run Branch。它通过与 Ticket Job 相同的 Candidate、Publication、Fresh Acceptance、Required Checks 和 Published-Head Gate 控制流，以 squash merge 进入 Run Branch；合并后不再复用，后续问题创建新的 Run Repair Job/PR。
_Avoid_: Run PR、Ticket PR、直接推送 Run Branch

**Run Acceptance Repair Loop（运行验收修复循环）**:
Run Acceptance 或已创建 Run PR 的 CI 产生可自动修复问题后，Controller 创建 Run Repair Job，并将原始 Acceptance Artifact 或 CI Evidence 交给复用的 Run Repair Thread。Development–Acceptance Engine 完成开发、Fresh Acceptance、Required Checks、Published-Head Gate 和 squash merge；Run Branch 更新使旧 Run Acceptance 与 Run PR Narrative 失效，Controller 随后启动新的 Run Acceptance Reviewer Thread，通过后再由新的 Run Publication Codex 生成或刷新正文。该循环持续到通过、需要人工决策或累计耗尽十次实际代码修复预算。
_Avoid_: 复用旧 Reviewer、直接修改 Run Branch、仅修复局部 Ticket diff

**Run Feedback Revision（运行反馈版本）**:
维护者通过 Final Revision Command 提交的 Run 级权威反馈及其稳定指纹。它进入 Run Repair Job 的 Effective Revision、Run Acceptance Artifact envelope 和后续 Run Acceptance 需求源，直到 Delivery Run 被批准或放弃；Controller 不总结、改写或静默丢弃该反馈。
_Avoid_: PR 普通评论、Parent Spec Revision、Controller 摘要

**Final Human Acceptance（最终人工验收）**:
Run Acceptance 与 Run PR Required Checks 全部通过后，Controller 首次向维护者请求的 Delivery Run 整体确认。维护者通过本地 `agent-run approve <run-id>` 显式批准；Controller 随后重新核对默认分支 live base、Run PR live head、Run Acceptance、Required Checks、Parent Spec Revision 与 Ticket Graph Revision，完全一致时 Publisher 才可使用普通 merge commit 和 `--match-head-commit` 合入默认分支。此前各 Ticket 的自动完成不触发逐票人工验收。
_Avoid_: Ticket 级确认、自动 merge 默认分支、Agent 语义范围判断

**Final Approval Command（最终批准命令）**:
维护者对一个已通过全部自动门禁的 Delivery Run 授予默认分支合并权限的本地显式命令 `agent-run approve <run-id>`。该授权只对命令执行时重新验证的 Run PR head 和有效 Revision 生效；状态漂移时命令拒绝合并并恢复自动处理或重新请求验收。
_Avoid_: GitHub Approve、标签触发、永久授权

**Final Revision Command（最终修改命令）**:
维护者通过 `agent-run revise <run-id> --message <feedback>` 提交的统一 Run 级人工恢复命令，既用于最终人工验收要求修改，也用于 Run Acceptance 返回 `human` 或累计修复预算耗尽。反馈原样形成新的 Run Feedback Revision、进入 Run Repair 输入，并显式开启新的十次 Run 修复预算窗口；修复后必须重新通过共享 Development–Acceptance Engine、全新 Run Acceptance、Run Publication 与 Run PR Required Checks。若反馈引起 Ticket Set 或依赖变化，Run 必须 fail closed 为 Unsupported Scope Change。
_Avoid_: Parent Spec 静默改写、直接修改 Run Branch、无限自动重试

**Final Abandon Command（最终放弃命令）**:
维护者通过 `agent-run abandon <run-id>` 永久终止尚未进入默认分支的 Delivery Run 的显式命令。Controller 执行 Run Abandonment Recovery、关闭或标记未合并的自动化 PR，并保留完整审计证据；暂时等待或普通修复不能使用该命令。
_Avoid_: 暂停、单 Ticket 阻塞、默认分支回滚

**Ticket Squash Merge（Ticket 压缩合并）**:
Publisher 在 Ticket PR 的 Published-Head Gate 通过后执行的固定合并方式。它使用 `--squash` 与 `--match-head-commit`，将 PR 公开分支上的 Publication 与 repair commits 作为一个新语义 commit 写入 Run Branch；首次推送前已压缩掉的本地 Candidate Commits 不会出现在 PR 页面。合并结果必须继续匹配 reviewed base、Publication tree 和语义标题。
_Avoid_: Rebase merge、普通 merge commit、发布前 Candidate 压缩

**Run Merge Commit（运行合并提交）**:
Run PR 通过最终人工整体验收后写入默认分支的普通 merge commit。其第二父历史保留 Run Branch 中各 Ticket squash commits，使系统既能按 Ticket 回滚，也能按整次 Delivery Run 回滚。
_Avoid_: Ticket Squash Merge、fast-forward、再次 squash 全部 Tickets

**Primary Ticket（主 Ticket）**:
一张 Ticket PR 唯一负责完成的 GitHub Issue，由 Ticket Job 身份确定，并通过 GitHub Development 原生关联与 PR 正文中的 `Primary Ticket` 引用共同表达。由于 PR 目标是非默认 Run Branch，Publisher 不依赖 closing keyword，而在合并后根据 Ticket Job 身份显式关闭该 Issue。
_Avoid_: Related Issue、Agent 猜测、一个 PR 关闭多张 Ticket

**Ticket Completion（Ticket 完成）**:
Ticket PR 通过 Fresh Acceptance、Required Checks 和 Published-Head Gate，并合入所属 Run Branch 后形成的完成状态。Publisher 随即显式关闭 Primary Ticket，并评论记录 Delivery Run、Ticket PR 和进入 Run Branch 的 commit SHA，同时说明该变更尚未进入默认分支；该关闭动作使 GitHub `blockedBy` 依赖自然解除。
_Avoid_: 合入默认分支、最终人工整体验收、closing keyword

**Ticket Completion Revision（Ticket 完成版本）**:
Run Acceptance 与 Run Repair Revision Snapshot 使用的确定性 Ticket 完成身份，只包含 Ticket number、integrated SHA、Effective Revision 以及已验收 base/tree，并按 Ticket number 数值排序。完整 Acceptance Record 继续保存在对应 Ticket Job 中，不嵌套复制到该版本身份。
_Avoid_: Ticket Completion、完整 Acceptance Record、Reviewer 文案

**Run Abandonment Recovery（运行放弃恢复）**:
Delivery Run 在进入默认分支前被人工取消或永久放弃时，由 Publisher 只重新打开该 Run 曾记录为由 Publisher 从 open 变为 closed、且尚未进入默认分支的 Tickets，并留下恢复原因和 Run 证据；运行开始前已关闭或由外部主体独立关闭的 Issue 不得被重开。普通返工或暂时阻塞不触发整批重开。
_Avoid_: Ticket 自动修复、Run 暂停、默认分支回滚

**Ticket Job（Ticket 任务）**:
一张 Ticket 在一个 Delivery Run 中唯一、持久且可恢复的工作身份。其内容变化、失败和普通修复只产生新的 Attempt，不产生第二个 Ticket Job、Ticket Branch 或并行 active PR；仅 post-merge、pre-completion Revision 漂移会保留已合并 PR 为 superseded integration，并为最新 Revision 顺序创建新的 active PR。Change Job Record 对每个 superseded integration 只保留旧 `pr_number`、`integrated_sha` 与 `effective_revision`，重复恢复不重复追加。
_Avoid_: Ticket、Attempt、Codex Thread

**Active Ticket Job（活跃 Ticket 任务）**:
Delivery Run 当前唯一获准进入开发、发布或验收循环的 Ticket Job。Controller 任意时刻最多激活一个，其他 Ticket 即使依赖已解除也仍保持等待。
_Avoid_: 所有 ready Ticket、并行 frontier、后台 worker 池

**Ticket Selection Order（Ticket 选择顺序）**:
多个 Ticket 同时具备执行资格时使用的确定性顺序：优先采用 Parent Spec 的 GitHub 原生 `subIssues` 顺序，缺少稳定顺序时按 Issue number 升序。Codex Worker 不决定该优先级。
_Avoid_: Agent 自主排序、依赖边、人工逐票选择

**Ticket Branch（Ticket 分支）**:
一个 Ticket Job 独有并在其整个生命周期中复用的开发分支。通常只对应一张持续更新的 active Ticket PR；post-merge、pre-completion Revision 漂移时分支身份保持不变，但已合并 PR 仅作为 superseded integration 历史，新 Revision 使用新的 active PR。
_Avoid_: Run Branch、Attempt Branch、临时 worktree

**Candidate Commit（候选提交）**:
Publisher 在每轮 Development Codex 编辑返回后，于 Change Job working branch 创建的不可变候选快照。Git 完整性检查和修复边界必须绑定其 SHA；每轮修复产生新的 Candidate Commit。它使用程序生成的临时 commit message，例如 `chore(ticket-12): candidate 2`，首次推送前会被压缩，不进入公开历史。
_Avoid_: Publication Commit、远端 PR head、Agent 自行提交

**Publication Commit（发布提交）**:
Candidate Commit 通过 Fresh Acceptance 后，Publisher 根据 Publication Artifact 保持已验收候选 tree 不变，以 Run Branch 的有效 base 为父提交并使用 Agent 编写的语义 commit message 创建待发布提交。Publisher 必须验证 Publication Commit 与已验收 Candidate Commit 的 tree 相同；随后 Published-Head Gate 将远端 PR head 绑定其准确 SHA。
_Avoid_: Candidate Commit、机械 checkpoint、远端 PR head

**Development Summary（开发摘要）**:
Development Codex 在 Development Attempt 结束时返回的普通、可读最后回复。它说明完成的工作、运行过的验证、已知阻塞及值得人类注意的后续影响，但不要求符合 JSON Schema，也不构成候选状态、测试通过或完成的权威证明。
_Avoid_: Publication Artifact、Controller Evidence、完成证明

**Publication Artifact（发布产物）**:
Candidate Commit 通过 Fresh Acceptance 后，由独立、只读的 Publication Codex 根据当前 Issue、已验收的最终 diff 与完整 Fresh Acceptance 证据生成的小型结构化语义产物，提供语义 commit message、`type(scope): user-facing outcome` 格式的 PR title 和符合统一 PR Narrative 的完整正文。需求、最终方案、用户影响或验证证据变化时，Publication Codex 必须重新读取当前事实并更新产物；Publisher 只校验结构并执行写入，不改写 Agent 内容。
_Avoid_: Development Summary、独立发现工作流、验证记录

**PR Narrative（PR 语义正文）**:
Publication Codex 为 Ticket PR、Run Repair PR 与 Run PR 编写并保持准确的统一耐久说明，顶部身份分别使用 `Primary Ticket: #N`、`Delivery Run: <id>`、或 `Parent Spec: #N` 加 `Delivery Run: <id>`，正文均包含非空的 `What Problem This Solves`、`Why This Change Was Made`、`User Impact` 和 `Evidence`，Ticket PR 还可包含 Cross-Ticket Note。`Evidence` 只记录独立 Fresh Acceptance 或 Run Acceptance 三条 lane 的真实证据，每条以“场景 → 实际操作或命令 → 可观察结果”表达；CI、Candidate、SHA、门禁和生命周期等机器事实由 Publisher 的 Agent Run Status 评论呈现，不进入叙事正文。UI、交互或可视输出变化时才加入 before/after 图片或视频，无内容的可选段落必须整个省略。
_Avoid_: Files changed 复述、通用 checklist、Agent 内部元数据

**Cross-Ticket Note（跨 Ticket 说明）**:
Development Codex 在 Ticket PR body 的可选 `## Cross-ticket notes` 段落中记录的非权威开发上下文，每项使用 `Affects #N: ...` 明确引用目标 Issue；没有相关信息时省略整个段落。后续 Development Codex 根据 Prompt 中的 GitHub 上下文约定，使用只读 GitHub 自主查找和阅读其认为相关的 Issue、PR 与 commit；Controller 不限制、解析或路由这些说明，它们也不修改 Issue、依赖图或 Acceptance Criteria。
_Avoid_: 正式需求增量、Issue 修改、验收门禁

**Publication Metadata（发布元数据）**:
Publisher 为每张已创建的开放自动化 PR 维护的一条 `Agent Run Status` 评论，原地更新 scope、Candidate/base、Fresh Acceptance、三条验收 lane、Required Checks 和 next action。它只是本地权威状态的简洁远端投影，不进入 Agent 编写的 PR 语义正文，也不发布完整 Acceptance Artifact、Acceptance Record、repair input 或其他嵌套 JSON。
_Avoid_: PR Narrative、Agent 自述、重复验证说明

**Fresh Acceptance（独立验收）**:
Candidate Commit 创建后、生成 Publication Artifact 与 Publication Commit 前，由 Controller 按 Change Job Contract 启动全新 YOLO Reviewer Thread，在独立 Validation Checkout 中针对准确的 base SHA、Candidate Commit SHA、有效 Revision、需求源和验收标准执行实际使用与代码审查。正常新一轮验收使用新的 Thread 和 checkout，不继承 Development Thread、旧 Reviewer Thread 或任何开发者自我判断；唯一例外是 Human Blocker resume：它复用刚刚被阻塞的 Reviewer Thread，但仍重新创建一次性 Validation Checkout 并重新核验。它可为验证构建和运行测试，但不得修复源码、测试、配置或 `.gitignore`，发现的问题必须进入 Acceptance Artifact。只有其通过结果、Publication Commit 与已验收 Candidate 的 tree equality 以及随后 Published-Head Gate 同时有效，目标 PR 才可进入 Run Branch。
_Avoid_: 开发者自测、第二次 GitHub Codex Review、仅测试通过

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
顶层 Codex 判断必须由人提供产品决策、外部权限、敏感凭据或不可替代外部操作才能继续时的最小结构化请求。Fresh/Run Acceptance 以一个或多个 `blocked` lane 的 evidence 表达；Development 使用 `result_kind: "human_blocker"`、`summary: null` 和非空 `human_blockers`，Publication 使用对应五字段 flat wire contract。Controller 只保存、展示与在 resume 时原样传回 blocker；可选的不可变 Human Response 仅绑定当前 Job Generation，按顺序进入后续 Development 与 Fresh Acceptance，不修改 Issue、不触发 Requeue，也不与 Run Feedback Revision 混用。每个 response 最多 8 KiB；同一 Generation 的序列不按容量截断，替换 Generation 会从空序列开始，绝不向新 Generation 注入旧响应。恢复成功后当前 blocker 告警会清除，原始 blocker/response 尝试按顺序保留。
_Avoid_: Controller 诊断、subagent 事件、自动重试策略、笼统失败摘要

**Human Response（人工响应）**:
维护者通过 `agent-run resume <run-id> --message "..."` 为当前 Human Blocker 提供的原样、不可变
上下文。它 trim 后必须非空且最多 8 KiB，按顺序只绑定当前 Job Generation，进入后续 Development 与
Fresh Acceptance；它不适用于 `execution_failed`、不修改 Issue、不触发 Requeue，也不等同于 Run
Feedback Revision。替换 Generation 从空序列开始。
_Avoid_: Run Feedback Revision、Issue 编辑、跨 generation 上下文

**Review Finding（审查发现）**:
Acceptance Artifact 的一个 lane 中可由 Development Codex 独立修复和验证的问题单元。它以一条自包含文本说明具体问题、代码或行为证据、必须达到的结果以及验证方式；人工产品决策、外部权限或不可替代操作进入该 lane 的 `blocked` evidence，不伪装成 Finding。
_Avoid_: 风格意见、无证据猜测、实现方案命令

**Acceptance Repair Loop（验收修复循环）**:
Fresh Acceptance 要求修改时，Controller 将原始 Acceptance Artifact 作为新的 Development Brief 输入，在同一 Change Job、working branch 与 Development Thread 上开始新的 Development Attempt。修复后必须重新自测、通过 Git 完整性检查、生成新的 Publication Artifact 和 Publication Commit，并由新的 Fresh Acceptance Reviewer Thread 验收；Reviewer 本身不修改代码。
_Avoid_: Reviewer 直接修复、创建新 Ticket Job、复用旧验收结论

**Published-Head Gate（已发布 Head 门禁）**:
Fresh Acceptance 通过后，Publisher 推送同一个 Publication Commit，并由 Controller 确定性验证目标 PR 的 live head、base、有效 Revision、Required Checks、mergeability 与已验收记录完全一致。它不进行第二次 Codex 语义审查；任何 head、base 或 Revision 漂移都会使旧验收失效，最终合并使用 `--match-head-commit` 绑定已验收 SHA，并在完成 Ticket 前验证 integrated commit 的 parent、tree 和标题。若远端已合并但本地同步中断，恢复必须先对齐本地 Run Branch，再完成 Ticket。
_Avoid_: Fresh Acceptance、GitHub PR Exact-Head Review Loop、智能代码判断

**Git Integrity Check（Git 完整性检查）**:
Publisher 在创建、压缩、推送和合并候选时执行的确定性 Git 校验，包括 clean tree、预期 HEAD、base 绑定、压缩前后 tree equality、远端 lease 和 live head equality。它不运行仓库测试，也不判断实现是否正确。
_Avoid_: Hosted CI Gate、Fresh Acceptance、本地 changed-surface validation

**Hosted CI Gate（托管 CI 门禁）**:
任何 Published PR 推送后由 GitHub Ruleset 针对 live head SHA 声明的 Required Checks 自动测试门禁，适用于 Ticket PR、Run Repair PR 和 Run PR。MVP 不实现 Controller 本地测试配置、changed-surface validation 或 CI 发现窗口；存在 Required Checks 时必须等待并读取结果，失败时把对应日志交回对应 Development Thread，全部通过后才允许后续合并或最终人工验收。目标 branch 没有 Required Check 时按仓库无强制 CI 处理，不构成阻塞；普通非必需 Check 不参与自动门禁。Publisher 不得使用 Ruleset bypass 权限绕过 Required Checks。
_Avoid_: Development Codex 自测、Controller 本地测试执行器、Fresh Acceptance

**Acceptance Record（验收记录）**:
Development–Acceptance Engine 在独立验收后本地持久化的权威记录，将唯一一份 Acceptance Artifact 绑定到 acceptance scope、reviewed base、已验收 Candidate 或 Run head、对应 tree 或预期合并结果、有效 Revision 和 Reviewer 身份。Controller 从 Artifact 的三个 lane 推导通过、返工或人工阻塞；只有仍然 current 的三个 lane 全部 `pass` 可以授权 Publication，后续 Attempt 替换当前 Record，历史只按恢复需要有界保留，GitHub 只接收简洁的 Agent Run Status 投影。
_Avoid_: Publication Metadata、PR 语义正文、永久适用于整张 PR 的结论

**Agent Invocation（Agent 调用）**:
Controller 对一次阶段级 Codex 调用的持久记录。Ticket、Parent-only 和 Run Repair 的
Development、Fresh Acceptance 与 Publication Invocation 都在首个 Output Attempt 前成为 active，
并绑定 Work Subject、Generation、输入指纹与机械 Currentness Boundary；记录只保存输入指纹和
有界边界事实，不保存 Prompt、transcript 或 Acceptance Artifact。`thread.started` 在进程运行中
立即保存。零退出但不符合完整阶段 contract 的输出可在同一 Thread 中最多修复两次；repair
checkout 只读，且不增加领域 Development、Validation 或 Publication Attempt。进程失败、缺失或
不匹配的 Thread 只结束当前 Invocation，不自动重试或创建替代 Thread。操作者可默认 Resume 原
Thread，或用 `--new-thread` 明确以标准阶段 Prompt 新开 Thread。
_Avoid_: Development Attempt、自动替代 Thread、领域 retry

**Output Attempt（输出尝试）**:
一个 Agent Invocation 内的一次 `codex exec` 进程执行。初始输出是第一个 Output Attempt；仅当进程
零退出、Thread 身份正确而本地完整 contract 不合法时，Controller 才在同一 Thread、只读 checkout 中
最多追加两次机械 Output Repair。Repair 不产生新的 Invocation、不消耗领域 Development、Validation 或
Publication attempt，也不适用于进程、凭据、sandbox、timeout、signal 或 Thread 错配失败。
_Avoid_: Invocation Resume、智能重试、独立持久 journal

**Invocation Resume（调用恢复）**:
维护者以 `agent-run resume <run-id>` 为当前 `execution_failed` 或 Human Blocker Invocation 创建的
successor Invocation。存在可恢复 Thread 时默认复用它；`--new-thread` 或无可恢复 Thread 时才以该阶段
完整标准 Prompt 新开 Thread。Resume 成功与否不改变 Job Generation，且在 preflight 发现 Currentness
Boundary 已 stale 时不启动 Codex，只进入 `requeue_required`。
_Avoid_: Output Repair、Publisher/check 幂等恢复、隐式 Requeue

**Ticket Repair Budget（Ticket 修复预算）**:
一个 Ticket Job 在 Fresh Acceptance 或 CI 失败后最多可触发十次自动修复 Development Attempt。等待 CI、重复读取状态或对同一未变化 SHA 重新检查不消耗预算，只有实际启动并允许修改代码的修复 Attempt 才计数。预算耗尽、Git 完整性检查无法通过或 CI 无法自动修复时，Ticket 转为 `ready-for-human`；Controller 继续推进不依赖该 Ticket 的其他任务。
_Avoid_: CI 等待次数、同一 SHA 重复审查、无限重试

**Ticket Resume Command（Ticket 恢复命令）**:
维护者通过 `agent-run resume <run-id>` 恢复当前 failed 或 Human Blocker Invocation。若 preflight
发现 Ticket Content Revision、Parent Revision 或适用 base 已变化，Controller 不启动 Codex，也不
复用原 Ticket Job、Ticket Branch 或 Development Thread；它停在 `requeue_required`，维护者需执行
`agent-run requeue <run-id>` 创建新的 generation。内容未变化时 Resume 仍可复用当前 Invocation 的
Thread；它不重置修复预算。
_Avoid_: 隐式 Requeue、无内容变化重试、Run Feedback Revision

**Progress Exhaustion（推进耗尽）**:
Delivery Run 中已不存在可激活的 Ticket Job，但 Ticket Set 尚未全部完成的状态。单张 Ticket 转为 `ready-for-human` 不会立即造成推进耗尽；Controller 仍应完成所有不依赖该 Ticket 的可执行工作，只有进入推进耗尽后才汇总阻塞项并请求人工介入。
_Avoid_: 单 Ticket 失败、正常依赖等待、最终整体验收

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

**Unsupported Scope Change（不支持的范围变化）**:
运行中 observed Ticket Set 或 `blockedBy` Graph Revision 与 accepted revision 不一致时的 fail-closed 状态。Controller 保留 accepted/observed revision、变化摘要和 observed graph；该状态不运行 Codex、不新建 Thread、不 Requeue、不继续交付，也不执行 Publisher mutation。操作者只能查看状态/历史、恢复 GitHub 原图或放弃当前 Run。
_Avoid_: Scope Impact Assessment、结构确认、自动吸收、自动 Requeue
