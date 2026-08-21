# Agent 交付自动化

本上下文描述以结构化 Agent 工作结果驱动、由确定性程序控制外部写入的自动交付流程。

## Language

**Codex Worker（Codex 工作器）**:
在隔离工作区内进行规划、代码编辑、验证或审查，并返回结构化 Artifact 的智能执行者。它不持有 GitHub 写凭证，也不具备外部交付状态的变更权限。
_Avoid_: GitHub Bot、Publisher、Mutation Authority

**Worker 只读凭据续签（Worker Read Credential Renewal）**:
一个最长三小时的 Codex Worker 通过临时 `gh` adapter 请求 GitHub 读取；Controller 以短期、只读的 GitHub App installation token 执行经过固定读取规则校验的请求。每次读取有界超时，Worker 结束或凭据续签耗尽时 Controller 会清理仍在执行的读取进程。Controller 在 token 即将失效时自动换发；换发出现短暂失败时在十分钟内有界重试。只有旧 token 已失效且重试仍失败，Worker 才以可恢复的凭据失败暂停。Worker 不获得 token、App 私钥或 Publisher 写凭据。
_Avoid_: 延长 installation token 的有效期、向 Worker 暴露 App 私钥、无限重试、直接中断

**Agent Artifact（Agent 产物）**:
Codex Worker 返回的结构化意图、判断与证据。它可以包含代码变更的语义说明及待发布内容，但本身不授权任何外部写入或完成状态。
_Avoid_: GitHub 状态、完成证明、自由文本交接

**验收 Finding（Acceptance Finding）**:
独立验收 Agent 在其负责的验收 lane 中发现的、必须在当前 Change Job 中处理的问题。每条 Finding 是符合 `问题：…；证据：…；必须修复：…；复验：…` 格式的非空字符串；它表达一个必须处理的问题，任一 Finding 都使所属 lane `fail`。只有同时处于当前 Review Boundary 内、违反当前需求或造成明确工程风险、有可复核证据、且能由当前 Job 修复的问题才构成 Finding。E2E、Standards、Spec 三个 lane 各自保存 Findings，Controller 不设顶层 Finding 汇总或 Agent 输出的 verdict：任一 lane 的 Finding 非空即将其原样交回 Development。Reviewer 应一次报告当前审查中已经可证明的全部 Findings，不得故意逐轮滴漏；这不要求为追求穷尽而扩大 Review Boundary 或进行无边界探索。纯维护性建议、可选重构、文件大小偏好和其他非阻塞观察不得进入 `findings`，可按 Non-blocking Observation 写入相关 lane 的 `evidence`；需要产品决定、权限、凭据或不可替代外部操作时进入 `blocked` evidence，而非 Finding。
_Avoid_: 非空 Finding 的 pass、无行动依据的泛泛建议、Controller 解释或重写 Finding、重复写入多个 lane、故意逐轮滴漏

**Deferred Scope Note（延期范围说明）**:
Reviewer 在当前 Ticket 审查中实际遇到、但已由明确 sibling/follow-on Issue 负责的范围边界说明。它只以 `Deferred to #N：…` 写入最相关 lane 的 `evidence`，不得进入 `findings`、改变 lane 状态或触发 Repair；Development 遇到同类边界时可写入 Development Summary。Agent 不为生成此说明而遍历完整 Ticket Set，也不负责创建、修改或维护对应 Issue；Publication 不把它叙述为当前 Ticket 的交付成果。
_Avoid_: 非阻塞 Finding、每轮枚举 sibling、由 Agent 创建 Issue、把 deferred 当作当前交付

**Non-blocking Observation（非阻塞观察）**:
Reviewer 在当前审查中实际形成的纯维护性建议、可选重构、文件大小偏好或其他不影响当前 Ticket 接受结论的意见。它只以 `Non-blocking observation：…` 写入最相关 lane 的 `evidence`，不得进入 `findings`、改变 lane 状态或触发 Repair；Agent 不负责为其创建或维护 Issue，Publication 不把它叙述为当前 Ticket 的交付成果。
_Avoid_: Acceptance Finding、Human Blocker、强制寻找建议、以建议触发 Repair

**Review Boundary（审查边界）**:
一个 Change Job 中可产生必做开发工作或 blocking Finding 的唯一语义范围：Ticket Job 只包含 Ticket Contract 及其 Candidate 对新增或改变路径直接造成的工程风险；Parent-only 覆盖完整 Parent，Run Repair 与 Run Acceptance 覆盖完整 Parent、Ticket Set、累计变更和预期合并结果。Ticket Job 中的 Parent Context 与 sibling/follow-on Issue 只用于理解背景、解释当前 Ticket 明确引用且完成其 AC 所必需的约束，以及 deferred 归属；它们不能自行增加当前 Ticket 的交付项。
_Avoid_: 把完整 Parent 当作 Ticket 范围、按文件列表划定范围、把未来 Ticket 工作当作当前 Finding

**Ticket Contract（Ticket 合同）**:
Primary Ticket 当前 title/body 及其中 Acceptance Criteria 形成的唯一立即开发与验收合同。Ticket 对 Parent 的明确引用只引入完成本 Ticket AC 所必需的输入或行为语义，不吸收 Parent 中可独立交付的 sibling/follow-on 能力。
_Avoid_: 完整 Parent Spec、Issue 评论、sibling Ticket、未来状态机

**Parent Context（Parent 背景）**:
Ticket Job 为理解整体目标、术语和 Ticket Contract 而读取的 Parent Issue 内容。它帮助 Agent 正确解释当前 Ticket，但本身不是该 Ticket 的需求源、验收清单或新增工作授权。
_Avoid_: Ticket Contract、Parent-only Delivery 范围、Run Acceptance 范围

**最小充分改动（Minimum Sufficient Change）**:
能够完整满足当前 Ticket 验收条件、处理本次改动直接造成的工程风险，同时不增加无关行为、状态、依赖、配置、公开入口或抽象层的最小连贯改动。它优先沿用直接适用的现有模块和约定；只有当前正确性、可测试性、已经存在的具体重复或仓库既有设计确有需要时，才进行必要的局部重构。最小不等于机械追求代码行数最少，模块化也不等于增加文件、转发层或为未来需求预留通用框架。
_Avoid_: 最少代码行、顺手重构、未来扩展点、假想复用、为拆分而拆分

**Prompt 用语约定（Prompt Language Convention）**:
面向 Codex Worker 的角色、责任、范围、完成条件和工作步骤默认使用清楚直接的中文，但项目中已稳定使用的英文领域术语保持原名，不机械翻译，也不在同一 Prompt 中为同一概念交替使用中英文名称。`Ticket`、`Parent Issue`、`Acceptance Criteria`、`Development`、`Repair`、`Fresh Acceptance`、`E2E`、`Standards`、`Spec`、`Finding` 与 `blocker` 可直接使用；必须与程序、JSON、命令或 skill 精确匹配的名称保留原文并使用反引号。对不稳定、少见或容易误解的英文表达，直接用中文说明所需行为，不以术语本身代替工作要求。
_Avoid_: 同义词漂移、中英文名称交替、未解释的生僻术语、把抽象名词当作完成标准、翻译机器字段

**验收 Lane 状态（Acceptance Lane Status）**:
每个 E2E、Standards、Spec lane 独立输出 `status`、`evidence` 与 `findings`。`pass` 表示该 lane 已完整执行且 Findings 为空；`fail` 表示其 Findings 非空；`blocked` 表示该 lane 因权限、凭据、产品决策或不可替代外部操作而无法形成结论，Findings 必须为空且原因写入 evidence。Controller 只从三个 lane 推导结果：任一 `fail` 回到 Development；没有 `fail` 而存在 `blocked` 时成为 Human Blocker；三个均 `pass` 才通过验收。
_Avoid_: 顶层 verdict、pass 携带 Finding、fail 没有 Finding、blocked 同时产出部分 Finding

**Development Brief（开发简报）**:
提供给 Development Codex 的最小语义输入，包括 Parent Issue URL、适用时的当前 Ticket URL、不可重建的原始失败证据、GitHub 上下文约定和输出要求。有当前 Ticket 时，其 title/body 是唯一立即交付合同，Parent 只提供 Review Boundary 允许的背景与约束；Parent-only 与 Run Job 则按各自 Review Boundary 使用 Parent。Issue 评论、历史 PR、开发者总结和旧 Artifact 仅是调查线索；Codex 从 worktree 与只读 GitHub 自主获取事实，Controller 不注入内部运行账本。
_Avoid_: Change Job Record、完整环境快照、实现计划

**Delivery Hygiene（交付卫生）**:
Development 与 Repair 在当前 checkout 中承担完整交付整理责任：检查全部未提交内容，保留本任务必须交付的代码、测试、文档和配置，删除本次产生的临时、构建和测试产物；仅长期、可再生且不应版本控制的项目产物可进入 `.gitignore`。Fresh Acceptance、Run Acceptance 与 Publication 只能清理自己创建的验证或临时产物，不得整理交付内容或修改源码、测试、配置和 `.gitignore`。任一 Codex 在 checkout 外创建的临时路径必须可定位、只服务本次任务并在完成前清理，不得进行宽泛删除。Codex 不提交，最终 Git/GitHub 写入仍属于 Publisher。
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
Codex 以 YOLO 运行，并可自由读写宿主文件系统、联网及使用真实 Git CLI；Execution Guard 不提供通用 filesystem、network、审批或命令隔离。Worker 的 `gh` 是临时 adapter：只把固定的只读 GitHub 请求交给 Controller，token 不进入 Worker；这是一条凭据边界，不是对 Worker 其他命令或网络的 allowlist。Execution Guard 还通过只读权威 Git metadata 和不向 Worker 注入 Publisher 写凭据保留 Mutation Authority；Worker 启动前会拒绝 local Git config 中带 userinfo 的 HTTP(S) remote URL，并只返回不含 URL 或凭据的固定错误，不改写权威 config。该边界不承诺抵抗恶意进程、主动凭据搜索、宿主污染或数据外泄。
_Avoid_: hardened security sandbox、恶意代码隔离、全局 Git 配置、Publisher 权限

**Trusted Subagent Contract（受信任 Subagent 契约）**:
Development 与 Repair Codex 先执行 Review Boundary 的 scope triage，再按当前改动风险选择 self-preflight、定向审查或 `code-review` skill；普通局部改动与已有明确 Finding 的窄修复不固定派发整套预审，高风险或跨模块改动则应在代码稳定后取得足够的开发侧审查。Fresh Acceptance 与 Run Acceptance 仍须形成独立的 E2E、Standards、Spec 三条 lane；Prompt 以 `code-review` skill 作为 Standards/Spec 的推荐 SOP，并让 E2E 默认承担代码冻结后的完整运行验证，但不规定或声称 Controller 能审计精确调用次数、嵌套方式或命令顺序。Controller 只校验父 Reviewer Thread 新鲜性、三 lane 的 status、evidence、findings 与外层 SHA/Revision 绑定；内部 subagent 的人工阻塞由顶层 Codex 写入对应 lane 的 `blocked` evidence，只有顶层结构化输出可传给 Controller。
_Avoid_: 固定每轮双预审、Controller 内部 Agent 编排器、subagent provenance ledger、把 Prompt SOP 当作可审计调用图、父 Agent 自签

**Controller（控制器）**:
本地 `agent-run` 单进程中的确定性编排层。它读取 GitHub 与本地事实、维护状态机和 Revision、选择可执行 Job、启动 Codex Threads、校验 Artifacts、执行预算与门禁，并调用 Publisher 完成允许的写操作；它不替 Agent 做需求、代码或修复方案的语义判断。
_Avoid_: Codex Worker、独立 daemon、GitHub Mutation Authority

**Run 内部监督（In-Run Supervision）**:
一次由维护者显式启动或恢复的 `agent-run run`，在可自动判定的远端异步边界（例如 Required Checks、GitHub 事件最终一致性）内自行等待、退避重试和重新读取权威事实；维护者不为普通等待另行启动 watcher 或重复输入同一命令。GitHub 读取或对账的未知非零退出默认进入有界监督，Controller 只保存经脱敏、有界的错误证据，不从 `gh` stderr 推断网络、认证、权限、代理或其他具体原因。只有结构化远端事实已证明 Publisher intent、身份、head/base、检查、状态或关闭证据矛盾时，才转换为 Human Blocker；最终人工批准仍是独立授权边界。
_Avoid_: 维护者轮询 CI、常驻的第二套控制器、自动越过 Final Human Acceptance

**公开生命周期命令（Public Lifecycle Command）**:
维护者用于创建或推进 Delivery Run 的稳定交互入口。`start` 仅创建或返回本地 Run 记录及其受管 Run Branch，不推进生命周期；`run` 是唯一的自动生命周期入口。Development、Acceptance、Publication 与内部等待只是 Controller 的阶段，不要求也不允许维护者把它们作为独立流程手工串接。`resume`、`approve`、`revise`、`requeue` 与 `abandon` 只在各自明确的失败、授权或恢复边界执行；监督超时可由同一 Parent 的显式 `run` 或 `resume` 恢复。
_Avoid_: 手工反复执行内部阶段、把 `deliver` 当作公开工作流、以命令顺序替代 Controller 状态机

**监督截止时间（Supervision Deadline）**:
Run 内部监督按远端状态类别采用有限等待预算。预算内保持自动轮询与重对账；到期时保存最后的权威证据并进入监督超时暂停，而不是执行失败或假装通过。GitHub 只读状态或事件收敛的默认预算为 10 分钟，Required Checks 的默认预算为 45 分钟；其他类别须有自己的明确预算。
_Avoid_: 无限占用进程、把超时吞成 pass、把正常 pending 伪装成需要外部授权的 Human Blocker

**监督超时暂停（Supervision Timeout Pause）**:
可自动判定的远端异步状态在其监督截止时间内仍未收敛时，Run 保存最后的权威证据并退出，等待维护者显式继续；它不要求维护者提供产品决策、权限、凭据或其他额外操作。`status` 与 `history` 在超时暂停时显示 `agent-run resume <run-id>`；同一 Parent 的显式 `run` 也可重新读取权威状态并开始新的对应等待窗口。活跃前台进程仍自行监督，维护者不应据此重复输入命令。超时 `resume` 不接受 Human Response 或新的 Agent Thread，且不会创建 Worker、PR 或 merge。
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
当 Parent Issue 没有任何 Child Ticket 时使用的轻量完整交付：Publisher 创建与 Parent Issue 原生关联的唯一 Parent Branch 与直达默认分支的 Parent PR。它仍遵循共享的 Candidate-first、Fresh Acceptance、Required Checks 和 Published-Head Gate；`agent-run approve <run-id>` 只授予一次合并权限，Development–Acceptance Engine 随后重新核对当前 Parent Revision、验收记录、检查、默认分支与 PR head，并通过 Publisher 以普通 merge 合并。已合并但 closeout 响应丢失时，共享 Engine 只重试幂等审计评论和关闭，不得再次合并。
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
全部 Ticket Completion 后、首次创建 Run PR 前以及任何 Run Branch 或默认分支更新后执行的独立整体验收。它的对象是准确默认分支 head `D` 与准确 Run Branch head `R` 的预期合并结果：该结果必须可合并且满足完整 Parent Spec。正常 Attempt 使用全新 YOLO Reviewer Thread 和独立 Validation Checkout，针对 Parent Spec、Run Feedback Revisions、完整 Ticket Set、`D`、`R`、预期合并结果、累计 diff 和各 Ticket Acceptance Records 进行检查；失败或 Human Blocker 只能通过显式 `resume` 继续同一 Thread，或用 `--new-thread` 新开 Thread，且仍重新创建一次性 Validation Checkout 并重新核验边界。完整 Acceptance 输出不合法时最多进行两次同 Thread、只读 Output Repair。Run Reviewer 不复用任何 Development Thread 或 Ticket Reviewer Thread，重点检查跨 Ticket 交互、整体需求遗漏、局部实现累计偏离和集成回归；`D`、`R` 或有效需求 Revision 漂移都会使结论失效，只有通过后才允许生成或刷新 Run PR Narrative。
_Avoid_: Ticket Fresh Acceptance、简单汇总各票 pass、最终人工验收、只审 Repair diff

**Candidate Run Acceptance（候选运行整体验收）**:
Run Repair 尚未写入 Run Branch 时，对 repair Candidate `C` 与准确默认分支 head `D` 的预期合并结果执行的完整 Run Acceptance。它与正常 Run Acceptance 使用相同的 Parent 范围和 E2E、Standards、Spec lane；每个 Candidate 的 tree、预期合并 tree、requirements/currentness 边界、Reviewer 与结论形成不可变的轻量审计记录，由所属 Run 永久保留而不截断。其通过记录只有在 Candidate、Repair base、实际合入后的 Run Branch tree、默认分支、Parent Spec、Ticket Graph 与 Ticket Completion 全部严格一致时，才可由 Controller 提升为正式 Run Acceptance。否则该记录失效，不得复用。
_Avoid_: 仅审 repair diff、合入后无条件重跑同一 Reviewer、用候选通过直接授权发布

**Default Branch Drift（默认分支漂移）**:
默认分支 head 发生变化，使最终预期合并结果不再是 Reviewer 验收过的组合。Controller 自动废弃旧 Run Acceptance 与 Run PR Narrative，并首先在新 base 和当前 Run Branch 或 repair Candidate 上重新预演合并：预演成功时直接对该新组合执行全新整体验收，不创建 Repair；预演冲突或新组合验收失败时，才把原始证据交给 Run Repair Codex。活跃 Repair 中再次发生默认分支漂移时，Controller 固化已有 Candidate、保留 Repair Thread 与物理 Integration-repair Worktree，并按新 base 重新准备集成现场；Run Branch、Parent、Graph 或 Ticket Completion 漂移则使 Repair Job Generation 失效，不自动迁移其 Candidate 或工作区。只有无法自动解决或耗尽预算时才请求人工介入。
_Avoid_: 把 base 漂移直接当作代码修复、将旧 repair 自动套用到新的交付边界、Ticket Content Revision、人工逐次确认、复用旧验收

**Run Publication Codex（运行发布 Codex）**:
Run Acceptance 通过后由 Controller 启动的只读 YOLO Codex，读取 Parent Spec、完整 Ticket Set、各 Ticket PR、准确累计 diff 与真实验证证据，生成符合统一 PR Narrative 的 Run PR title/body。正常 Run Publication Attempt 使用新的 Codex Thread；失败或 Human Blocker 只能通过显式 `resume` 继续同一 Thread，或用 `--new-thread` 新开 Thread，并重新读取权威状态后继续或再次报告 blocker。完整 flat contract 输出不合法时最多进行两次同 Thread、只读 Output Repair。它的职责只包含发布语义，不复用 Reviewer Thread、不执行验收、不整理 Candidate checkout；若自己创建 checkout 外临时路径，负责在完成前按 Delivery Hygiene 清理。Run Branch、默认分支 base、有效需求或证据变化后必须基于新状态重新生成。
_Avoid_: Run Acceptance Reviewer、Controller 拼接正文、Run Repair Thread

**Run Repair Thread（运行修复线程）**:
Delivery Run 独有并跨最终集成修复 Attempt 复用的持久 Development Codex Thread。它只接收 Run Acceptance Artifact、Run PR CI Evidence 或默认分支合并冲突证据，以及 Parent Spec、完整 Run diff 和当前 Run Repair checkout，不复用任何 Ticket Development Thread；无法恢复时按 Development Thread 的相同显式 Resume / `--new-thread` 规则停止或恢复。
_Avoid_: Ticket Development Thread、Run Reviewer Thread、人工修复会话

**Integration-repair Worktree（集成修复工作区）**:
一个活跃 Repair Cycle 唯一、持久且可写的受管工作区。默认分支与 Run Branch 的预期合并发生冲突时，Controller 在此准备真实三方合并现场；Repair Codex 可读取 Git 状态、编辑和测试以解决语义冲突，但不得提交、push、rebase、创建 PR 或合并。它与稳定 Run Branch 及每次一次性、只读的 Validation Checkout 严格分离；同一 Cycle 的后续 Acceptance Finding 返回该工作区继续前进式修复，即使已集成 Job 被轮转为新的 Job/PR 也不新建开发工作区。每次 Candidate Run Acceptance 结束后，Controller 清理其 Validation Checkout，而不是丢弃仍可能继续修复的 Integration-repair Worktree。
_Avoid_: 在 Run Branch 直接修复、让 Reviewer 读取开发中目录、让 Codex 自行准备或发布 merge、为每个 Finding 新建 worktree

**Run Repair Job（运行修复任务）**:
Repair Cycle 中一次可发布修复使用 Change Job Contract 创建的 Development–Acceptance Engine 实例。它以当前 Run Branch 为 base、原始失败 Artifact、CI Evidence 或冲突证据为修复输入；同一时刻最多一个 Run Repair Job 活跃。Job 未集成时，多轮 Development Attempt 复用该 Job 的 branch/PR；Job 已集成后，若同一 Cycle 的 Candidate revalidation 在新的 default head 上又产生 Finding，Controller 归档该 Job，并以新的 base、branch 和 PR 身份轮转出后继 Job，同时保留 Repair Cycle 的预算、Run Repair Thread 与 Integration-repair Worktree。
_Avoid_: Ticket Job、整个 Delivery Run 唯一 PR、独立修复流水线

**Repair Cycle（修复周期）**:
为解决同一准确 Run Acceptance、Run PR CI 或默认分支合并冲突问题而连续执行的有界修复工作；一个 Cycle 可依次包含多个 Run Repair Job，但任一时刻只允许一个活跃 Job。它在可修复 Finding、可修复 CI 失败或 Git conflict 出现时以零次修改开始；每当 Repair Codex 产出一个含实际交付树修改的新 repair Candidate，`code_modification_attempts` 加一，最多十次。冲突检测、worktree 准备、Reviewer 重跑、Required Checks 重试和无交付树修改的调查不计数。Candidate 或 repair PR 的 Required Check 只有同时得到 GitHub `FAILURE` 结论、准确绑定当前 PR head 的 completed Actions job，并由 job steps 证明失败仅发生在仓库 `pyproject.toml` 的 `tool.agent-run.required-checks.code-failure-steps` 所列 `workflow::name::step`，才被视为已明确且可由本次代码修复并返回同一 Cycle；pending、未知、缺少或矛盾的 job/step 事实、未配置 step、cancelled、runner 或暂时平台错误只进入 Controller 监督，不消耗预算。默认分支漂移以及已集成 Job 的 revalidation Finding 都不结束 Cycle 或重置预算；Candidate 被提升为正式 Run Acceptance、需要人工决定、预算耗尽，或 Run Branch/Parent/Graph/Ticket Completion 漂移时结束 Cycle。
_Avoid_: 用 Delivery Run 的历史累计修改数作为当前预算、把流程重试计作代码修改、base 漂移重置同一问题的预算

**Run Repair PR（运行修复拉取请求）**:
一个 Run Repair Job 独有并在该 Job 未集成前的多轮 Development Attempt 中稳定复用的 PR，其 head 是独立 run-repair branch，base 是 Run Branch。普通 Finding Repair 依次经过 Candidate、Candidate Run Acceptance、Publication、Required Checks 和 Published-Head Gate，以 squash merge 进入 Run Branch；默认分支与 Run Branch 的 Git 冲突则使用 Merge-resolution Candidate，repair PR 以普通 merge commit 进入 Run Branch，以保留已解决默认分支 head 的祖先关系。已合入的 PR 身份不可改写或复用；同一 Cycle 后续 revalidation Finding 由轮转后的新 Job/branch/PR 承载。
_Avoid_: Run PR、Ticket PR、将冲突解法 squash 到旧 Run head、直接推送 Run Branch

**Merge-resolution Candidate（合并解决候选）**:
默认分支 head `D` 与 Run Branch head `R` 发生 Git 冲突时，由 Publisher 根据 Integration-repair Worktree 的已解决文件树创建的受控双亲提交：其父提交准确为 `R` 与 `D`。它是 Candidate Run Acceptance 的候选对象；通过后，repair PR 只能以保留 `D` 祖先关系的普通 merge commit 进入 Run Branch。Publisher 必须验证实际 Run Branch 结果 tree 等于该 Candidate tree，且 `D` 仍为结果祖先，否则不得提升验收结论。
_Avoid_: Agent 自行提交 merge、squash resolution tree、隐式 rebase Run Branch、把冲突只当作文本补丁

**Run Acceptance Repair Loop（运行验收修复循环）**:
Run Acceptance 或已创建 Run PR 的 CI 产生可自动修复问题后，Controller 创建 Repair Cycle 与首个 Run Repair Job，并将原始 Acceptance Artifact 或 CI Evidence 交给复用的 Run Repair Thread。Development–Acceptance Engine 产出 Candidate；Candidate Run Acceptance 以完整 Parent 范围检查准确 default head 与 Candidate 的预期合并结果。repair PR 的 Required Checks 与 Published-Head Gate 通过并合入 Run Branch 后，Controller 只有在 Candidate tree、repair base、实际 Run Branch tree、default head、Parent、Graph 与 Ticket Completion 均严格保持一致时，才将该 Candidate Acceptance 提升为正式 Run Acceptance。已受控合入后若仅 default head 前进，Controller 在同一 Cycle/worktree 重新预演和验收：新的 Finding 轮转新 Job/branch/PR 并继续消耗同一预算；其他 promotion 边界失配则废弃 Candidate，进入新的 Run Acceptance Generation。Git 冲突修复使用 Merge-resolution Candidate 与普通 merge，其他 repair 使用 squash merge。该循环持续到 promotion、需要人工决策或当前 Repair Cycle 耗尽十次实际代码修复预算。
_Avoid_: 复用不等价的旧 Reviewer、直接修改 Run Branch、仅修复局部 Ticket diff、对同一已验收树无条件重跑 Reviewer

**Run Feedback Revision（运行反馈版本）**:
维护者通过 Final Revision Command 提交的 Run 级权威反馈及其稳定指纹。它进入 Run Repair Job 的 Effective Revision、Run Acceptance Artifact envelope 和后续 Run Acceptance 需求源，直到 Delivery Run 被批准或放弃；Controller 不总结、改写或静默丢弃该反馈。
_Avoid_: PR 普通评论、Parent Spec Revision、Controller 摘要

**Final Human Acceptance（最终人工验收）**:
Run Acceptance 与 Run PR Required Checks 全部通过后，Controller 首次向维护者请求的 Delivery Run 整体确认。维护者通过本地 `agent-run approve <run-id>` 显式批准；Controller 随后重新核对默认分支 live base、Run PR live head、Run Acceptance、Required Checks、Parent Spec Revision 与 Ticket Graph Revision，完全一致时 Publisher 才可使用普通 merge commit 和 `--match-head-commit` 合入默认分支。批准只绑定这一准确 head/base 与验收结果：同一 head/base 的 GitHub 收敛延迟由 Controller 自动监督，新 head 或 base 则使批准失效并要求新的最终人工验收。此前各 Ticket 的自动完成不触发逐票人工验收。
_Avoid_: Ticket 级确认、自动 merge 默认分支、Agent 语义范围判断

**Final Approval Command（最终批准命令）**:
维护者对一个已通过全部自动门禁的 Delivery Run 授予默认分支合并权限的本地显式命令 `agent-run approve <run-id>`。该授权只对命令执行时重新验证的 Run PR head、base 和有效 Revision 生效；同一事实集合的收敛延迟继续自动处理，新 head 或 base 则令授权失效并重新请求验收。
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

**Linked Branch（关联展示分支）**:
GitHub Issue 页面上可选显示的开发分支关联。它只帮助人阅读 Issue 与开发分支的关系，不证明远端 ref、PR 或 Delivery Run 的存在性；缺失或不可用不影响分支、PR、恢复或交付。
_Avoid_: 远端分支身份、PR 身份、恢复门禁

**Candidate Commit（候选提交）**:
Controller 在每轮 Development Codex 编辑返回后，于 Change Job working branch 创建的不可变候选快照。Git 完整性检查和修复边界必须绑定其 SHA；每轮修复产生新的 Candidate Commit。它使用程序生成的临时 commit message，例如 `chore(ticket-12): candidate 2`，首次推送前会被压缩，不进入公开历史。
_Avoid_: Publication Commit、远端 PR head、Agent 自行提交

**Forward Candidate Repair（前进式候选修复）**:
对已创建 Candidate 的验收或 CI Finding，Development Codex 只在同一 Change Job 的受管开发工作区修改当前文件树，可以恢复、删除或重写旧 Candidate 已引入的内容；它不得执行 `git commit`、`git reset`、`git rebase`、强推或任何 GitHub 写入。Controller 随后从该工作区创建一个新的不可变 Candidate Commit，并为其创建新的、一次性 Validation Checkout；旧 Validation Checkout 在验收结束后清理，Development Thread 与受管开发工作区在 Currentness Boundary 未漂移时继续复用。因而 Git 历史只前进，而最终 diff 可以比先前 Candidate 更小。
_Avoid_: 改写 Candidate 历史、为同一边界的每条 Finding 新开 worktree、复用旧 Validation Checkout、要求人工先还原文件、把可自动修复的 diff 收缩误报为 Human Blocker

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
Acceptance Artifact 的一个 lane 中可由 Development Codex 独立修复和验证的问题单元。它以 `问题：…；证据：…；必须修复：…；复验：…` 格式的非空字符串表达；所有 Finding 都要求修复，不存在建议型或非阻塞 Finding。人工产品决策、外部权限或不可替代操作进入该 lane 的 `blocked` evidence，不伪装成 Finding。
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
任何 Published PR 推送后由 GitHub Ruleset 针对 live head SHA 声明的 Required Checks 自动测试门禁，适用于 Ticket PR、Run Repair PR 和 Run PR。MVP 不实现 Controller 本地测试配置、changed-surface validation 或 CI 发现窗口；存在 Required Checks 时必须等待并读取结果，失败时保存 check 与 Actions job/step 的结构化证据，只有仓库配置明确声明的 code/test step failure 才交回对应 Development Thread，其他失败由 Controller 监督；全部通过后才允许后续合并或最终人工验收。目标 branch 没有 Required Check 时按仓库无强制 CI 处理，不构成阻塞；普通非必需 Check 不参与自动门禁。Publisher 不得使用 Ruleset bypass 权限绕过 Required Checks。
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
successor Invocation。对 `modification_budget_exhausted`，维护者显式执行 Resume 即可
创建新的 Ticket Repair Budget Window；它仍是同一 Job Generation、复用仍有效的 Thread、branch 与 PR，
但不把新 Attempt 伪装成旧窗口的第十一轮。`--new-thread` 或无可恢复 Thread 时才以该阶段完整标准 Prompt
新开 Thread。Resume 成功与否不改变 Job Generation，且在 preflight 发现 Currentness Boundary 已 stale
时不启动 Codex，只进入 `requeue_required`。
_Avoid_: Output Repair、Publisher/check 幂等恢复、隐式 Requeue

**Ticket Repair Budget（Ticket 修复预算）**:
一个 Ticket Job 的每个 Ticket Repair Budget Window 在 Fresh Acceptance 或 CI 失败后最多可触发十次自动修复 Development Attempt。等待 CI、重复读取状态或对同一未变化 SHA 重新检查不消耗预算，只有实际启动并允许修改代码的修复 Attempt 才计数。窗口耗尽时 Ticket 转为 `ready-for-human`，保留 Candidate、PR、findings 与历史；只有维护者显式执行 Resume 才能创建有编号的新窗口。Git 完整性无法通过或 CI 无法自动修复仍按各自失败语义处理；Controller 继续推进不依赖该 Ticket 的其他任务。
_Avoid_: CI 等待次数、同一 SHA 重复审查、无限重试

**Ticket Repair Budget Window（Ticket 修复预算窗口）**:
同一 Ticket Job Generation 内一次明确授权的、最多十次实际代码修复 Attempt 的审计单元。它由持久的窗口编号和当前窗口消耗次数表达；预算耗尽不会创建第十一轮自动开发。新的窗口只能由维护者显式执行 `agent-run resume <run-id>` 在 `modification_budget_exhausted` 边界创建，并复用仍 current 的 Job、PR、branch、Candidate、findings 和历史；Currentness Boundary 已 stale 时只允许 `requeue` 创建新 Generation。
_Avoid_: Job Generation、CI 等待窗口、隐式自动续期

**Ticket Resume Command（Ticket 恢复命令）**:
维护者通过 `agent-run resume <run-id>` 恢复当前 failed 或 Human Blocker Invocation；在唯一的
`modification_budget_exhausted` 边界，该显式命令会新建有编号的 Ticket Repair Budget Window。
若 preflight
发现 Ticket Content Revision、Parent Revision 或适用 base 已变化，Controller 不启动 Codex，也不
复用原 Ticket Job、Ticket Branch 或 Development Thread；它停在 `requeue_required`，维护者需执行
`agent-run requeue <run-id>` 创建新的 generation。内容未变化时 Resume 仍可复用当前 Invocation 的
Thread；它不重置既有窗口的消耗记录。
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
