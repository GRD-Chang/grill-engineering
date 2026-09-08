# Agent 交付自动化

本上下文描述以结构化 Agent 工作结果驱动、由确定性程序控制外部写入的自动交付流程。

## Language

**Codex Worker（Codex 工作器）**:
在隔离工作区内进行规划、代码编辑、验证或审查，并返回结构化 Artifact 的智能执行者。它不持有 GitHub 写凭证，也不具备外部交付状态的变更权限。
_Avoid_: GitHub Bot、Publisher、Mutation Authority

**Worker GitHub Read Broker（Worker GitHub 只读代理）**:
Codex Worker 通过普通命令形态的受控只读 `gh` 入口提交绑定当前 Repository identity 的固定 GitHub 读取请求；Worker 不需要知道入口背后的凭据代理机制。Controller 默认使用宿主已登录的 `gh` 凭据执行；写请求、认证请求和其他仓库请求在宿主执行前拒绝。需要独立最小权限身份的操作者可以通过一次性 CLI 配置改用专用只读 GitHub App。App profile 一旦存在就明确选择 App provider；配置损坏、私钥不可读、签名或权限校验失败时有界失败，不得静默回退到宿主 `gh`。该持久配置只记录 App ID、Installation ID 与仓库外私钥文件的路径，私钥内容不复制，短期 installation token 不落盘；App 模式保留 token 到期前续签、短暂失败有界重试和过期读取重试。无论凭据来源如何，Worker 都不获得 token、App 私钥或 Publisher 写凭据，Controller 对每次读取设置有界超时并在 Worker 结束时清理仍在执行的读取进程。
_Avoid_: 要求 Worker 理解 adapter、socket、PATH 或挂载细节、强制配置 GitHub App、每次导出环境变量、持久化 installation token、复制 App 私钥、向 Worker 暴露宿主凭据、允许任意 GitHub 请求、无限重试

**Agent Artifact（Agent 产物）**:
Codex Worker 返回的结构化意图、判断与证据。它可以包含代码变更的语义说明及待发布内容，但本身不授权任何外部写入或完成状态。
_Avoid_: GitHub 状态、完成证明、自由文本交接

**验收 Finding（Acceptance Finding）**:
独立验收 Agent 在其负责的验收 lane 中发现的、必须在当前 Change Job 中处理的问题。每条 Finding 是符合 `问题：…；证据：…；必须修复：…；复验：…` 格式的非空字符串；它表达一个必须处理的问题，任一 Finding 都使所属 lane `fail`。只有同时处于当前 Review Boundary 内、违反当前需求或造成明确工程风险、有可复核证据、且能由当前 Job 修复的问题才构成 Finding。E2E、Standards、Spec 三个 lane 各自保存 Findings，Controller 不设顶层 Finding 汇总或 Agent 输出的 verdict：任一 lane 的 Finding 非空即将其原样交回 Development。Reviewer 应一次报告当前审查中已经可证明的全部 Findings，不得故意逐轮滴漏；这不要求为追求穷尽而扩大 Review Boundary 或进行无边界探索。纯维护性建议、可选重构、文件大小偏好和其他非阻塞观察不得进入 `findings`，可按 Non-blocking Observation 写入相关 lane 的 `evidence`；需要产品决定、权限、凭据或不可替代外部操作时进入 `blocked` evidence，而非 Finding。
_Avoid_: 非空 Finding 的 pass、无行动依据的泛泛建议、Controller 解释或重写 Finding、重复写入多个 lane、故意逐轮滴漏

**Delivery Quality Floor（交付质量底线）**:
自动交付在优化 Token、时间或 Reviewer 数量时不得降低的完成约束，包括 correctness、安全、数据完整性、公开契约与完整 Parent Acceptance Criteria。没有当前失败链的风格偏好、可选重构和低风险维护性意见不属于质量底线，可以延后到 Run 级集中评估或作为 Non-blocking Observation 保留。
_Avoid_: 追求零意见、以无限审查换取主观完美、把低风险建议升级为完成阻塞

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
Controller 为一个 Change Job 创建并在 Development Attempt、Repair 与 Resume 间复用的专属 worktree。它与主工作区、其他 Change Job 和一次性 Validation Checkout 隔离；系统只允许该 Job 的 Codex 整理其未提交内容，并在 Candidate 创建后交由 Publisher 写入 Git 历史。系统运行约束禁止向其中人工混入无关改动。普通 `run`、`resume` 或自动 cleanup 发现其中存在 tracked modifications 或 untracked files 时必须 fail closed，保留工作区并给出路径、保留原因与可执行恢复动作，不得因 `execution_failed`、`blocked`、预算检查点或状态合同错误自动强制删除。
_Avoid_: 主工作区、共享 scratch worktree、Validation Checkout、以终态名称代替 dirty check、自动强制删除未提交交付成果

**Development Attempt（开发尝试）**:
Development Codex 在一个 Change Job 的准确 Effective Revision 上完成内部规划、代码编辑和开发验证的一轮工作。同一 Change Job 的各次 Development Attempt 复用其 Development Thread，通过新的 Turn 接收最新版 Development Brief 与 Acceptance Artifact；它直接产出 worktree diff 与 Development Summary，不经过独立 Planning Phase。一次 Attempt 首次分配时只占用一次 Development Budget；为完成该 Attempt 而创建的 successor Invocation、同 Thread Resume 或 Output Repair 均不重复计费。
_Avoid_: Change Job、Plan Artifact、Acceptance Attempt、Codex 进程启动次数

**Development–Acceptance Engine（开发验收引擎）**:
Controller 复用的单一自动变更循环：持久 Development Thread 修改 checkout，Publisher 创建 Candidate 与 Publication Commit，Fresh Reviewer 输出 Acceptance Artifact，可修复 findings 原样返回开发，正常 pass 或 Ticket 预算耗尽后的 Fallback Publication Receipt 允许 Publisher 推送普通 PR，随后等待 Required Checks、执行 Published-Head Gate，并按 Job 的合并策略完成合并；兜底 Ticket 只有在准确 head 形成 Deterministic Integration Record 后才取得合并权限。Ticket Job、Parent-only Delivery 与 Run Repair Job 只通过不同 Job Contract、Prompt、上下文、批准策略和完成规则使用该引擎，不复制控制流；Parent-only 的人工批准只授予一次合并权限，批准后的重新核验、合并与恢复仍由该引擎执行。
_Avoid_: Ticket 专用流水线、Run Repair 专用流水线、动态 Agent 编排

**Review Optimization Policy（审查优化策略）**:
降低 Token 与审查重复时遵循的变更优先级：能通过更准确的角色、范围和完成条件解决的问题优先只调整 Prompt；Prompt 无法机械保证的预算、currentness、结构化证据与路由才在现有 Controller 和 Artifact seam 上小幅补足；Development–Acceptance Engine、Candidate/PR/Required Checks/Run Branch 阶段、Publisher Mutation Authority 与最终 Run Acceptance 权威保持不变。Ticket、Run 与 Parent-only 的每次 Reviewer Invocation 都按 Prompt 调用 `code-review` skill，并自主决定本轮需要定向复核还是全量审核。每个新 Review Budget Window 的 Reviewer 1 建立当前基线；后续 Reviewer 直到有效 Run Policy Snapshot 推导出的本层上限，只额外获得紧邻上一轮 Acceptance Artifact 的完整原始内容及其角色化 review identity，Prompt 建议优先参考其中的问题及当前修复，但不要求逐项 closure，也不限制 Reviewer 的检查顺序、范围或新问题发现。Controller 不解析 Findings、不判断哪些已关闭、不生成 Finding Ledger，也不要求 Development 为每个 Finding 提交 disposition。每次 Reviewer 仍对准确当前 Candidate 或预期合并结果负责，并只交付新的 Acceptance Artifact；旧结论不能直接授权新 tree。
_Avoid_: 为成本优化复制流水线、用 Prompt 假装实现硬门禁、无必要重写成熟状态机、破坏 currentness 或发布权威

**Predecessor Defect Repair（前序缺陷修复）**:
Development Codex 在交付当前 Ticket 时实际发现前序已集成代码存在问题，可以直接在当前受管开发工作区中一并修复、验证并纳入当前 Candidate，不等待另开 Ticket、人工分派或前序 Job 恢复。Development Prompt 明确授予该判断与修复责任；这不要求 Agent 主动遍历历史寻找问题，也不授权新增与当前 Parent 无关的产品能力。修复内容进入当前 Ticket 的累计 diff、直接风险审查和最终 Run Acceptance。
_Avoid_: 发现问题后机械停工、为同一修复恢复已完成 Ticket、主动审计全部历史、借机扩大产品范围

**Change Job Contract（变更任务契约）**:
Development–Acceptance Engine 处理 Ticket Job、Parent-only Delivery 或 Run Repair Job 时遵守的确定性事实与任务特有规则，包括需求源、有效 Revision、base/head branch、目标 PR 类型、开发与 Reviewer Prompt、修复预算以及完成规则。Controller 固定这些内容，智能 Agent 不选择或修改。
_Avoid_: Development Brief、Agent Artifact、Controller 全局配置

**Development Thread（开发线程）**:
一个 Change Job 独有并跨 Development Attempt 复用的持久 Codex Thread。它保存该任务的开发与修复上下文，但每个 Turn 都必须重新提供当前权威 Issue 引用、准备好的 checkout 和适用的原始反馈证据；其身份由 Change Job Record 保存，不与任何 Reviewer Thread 共享。因 Human Blocker 或 `execution_failed` 暂停后，`resume` 默认复用原 Thread 与保留的工作区并让 Codex 重新核验；Thread 无法恢复时停止为 `execution_failed`，只能由维护者显式用 `--new-thread` 开启标准阶段 Prompt 的新 Thread。
_Avoid_: Development Attempt、Reviewer Thread、Delivery Run 全局会话

**Change Job Record（变更任务记录）**:
Controller 私有保存的 Change Job 身份、Development Thread 身份、Revision Snapshot、Git 基线、Attempt、PR、预算和幂等状态。Ticket Job、Parent-only Delivery 与 Run Repair Job 使用同一规范记录骨架和生命周期语义，各自特有数据位于明确的 job-specific 部分；Ticket Job Record 按 Ticket 身份持久保存，并与当前 `active_ticket_job` 指针分离，Controller refresh 可以切换 active，但不得删除仍属于 Parent Ticket Set 的 blocked 或 completed Job。Job-local `blocked_reason` 保存恢复授权所需的阻塞原因；顶层 diagnostics 只是可重建的当前展示，Controller refresh 会从未解除的 deterministic blocker 幂等重建 blocked 投影。Controller 在任何外部 mutation 前验证唯一规范结构；不符合时返回 `incompatible_run_state` 并要求重新创建或清理该 Run，不使用 schema 版本号、迁移器或兼容读取。Review Budget Window、Final CI-fix 与 Deterministic Ticket Fallback 采用同一次规范状态切换：旧 Run 不迁移、不推断剩余额度且不兼容读取，只有按新结构创建的 Run 才使用新策略。该记录用于恢复与校验，不作为需要 Codex 理解或复述的任务输入。
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
Development 与 Repair Codex 先执行 Review Boundary 的 scope triage，再按当前改动风险选择 self-preflight、定向审查或 `code-review` skill；普通局部改动与已有明确 Finding 的窄修复不固定派发整套开发侧预审，高风险或跨模块改动则应在代码稳定后取得足够的开发侧审查。Ticket Integration Gate、Run Acceptance 与 Parent-only 的 Reviewer Prompt 都要求 Reviewer 调用 `code-review` skill，并把对应 Ticket Contract 或完整 Parent Contract、准确当前验收对象与适用风险交给该审查。Reviewer 2+ 还收到紧邻上一轮 Acceptance Artifact 的完整原始内容、其角色化 review identity 和“优先参考上轮问题”的倾向，但 Reviewer 自主决定是否定向复核、全量审核或发现新问题。Controller 信任该 Prompt 合同，不审计内部 skill、subagent 数量、调用顺序或命令拓扑，只校验父 Reviewer Thread、结构化结果与外层 SHA/Revision 绑定。
_Avoid_: 固定每轮双开发侧预审、Controller 内部 Agent 编排器、Finding closure 状态机、subagent provenance ledger、把 Prompt SOP 当作可审计调用图、父 Agent 自签

**Controller（控制器）**:
本地 `agent-run` 单进程中的确定性编排层。它读取 GitHub 与本地事实、维护状态机和 Revision、选择可执行 Job、启动 Codex Threads、校验 Artifacts、执行预算与门禁，并调用 Publisher 完成允许的写操作；它不替 Agent 做需求、代码或修复方案的语义判断。
_Avoid_: Codex Worker、独立 daemon、GitHub Mutation Authority

**Runner Snapshot（Runner 固化快照）**:
本机用于执行 Controller 的不可变代码快照。其 content identity 只对非 editable 安装后 `agent_run` runtime tree 的规范相对路径和文件内容计算 SHA-256；console entry、shebang、virtualenv 路径、Python 与系统库、缓存、日志、时间戳和 Runner Provenance 不进入该身份。源码可以来自正式发布、Git revision 或包含未提交修改的本地开发目录；来源不限制其生命周期权限。源目录后续变化不会影响既有快照，只有用户显式构建并激活另一快照才会改变调用使用的 Runner。
_Avoid_: 可编辑源码环境、Git commit、当前源码目录

**Runner Provenance（Runner 来源）**:
说明一个 Runner Snapshot 的源码来自正式发布、Git revision 或本地源码快照的审计事实。它帮助用户理解和追溯 Runner，但不替代内容身份，也不单独决定该 Runner 能否执行生命周期命令。
_Avoid_: Runner Snapshot、Runner 内容身份、Delivery Run currentness

**Runner Generation（Runner 世代）**:
一次管理动作准备并作为整体激活的完整指针集合，包含 current Runner Snapshot 与可选 previous Runner Snapshot。安装器必须先在 Active Runner 之外创建完整 Generation，再以同一文件系统上的原子切换选为 active；current、previous 或命令入口不得分别原地更新。构建、Compatibility Check 或切换失败时，旧 Generation 及其 current、previous 整体保持不变。
_Avoid_: 分步更新 current 与 previous、半完成安装、可配置历史列表、每-Run Runner 绑定

**Active Runner（当前 Runner）**:
本机命令入口为 Controller 调用选择的完整 Runner Generation，并直接执行其中的 current Runner Snapshot。Runner 管理只能在 Runner Management Quiescence 中改变 Active Runner，因此不会替换活跃 Run Executor Session 正在使用的 Runner；后续重新推进既有 Delivery Run 时使用届时的 Active Runner，系统不绑定旧 Snapshot、不迁移旧状态，也不承诺跨 Runner 状态兼容。
_Avoid_: 当前 Git checkout、自动跟随源码、运行中热更新、每-Run Runner 绑定、状态兼容层

**Runner Build（Runner 构建）**:
用户在自己选择的源码目录中显式运行该目录的源码安装器，将目录当前实际内容冻结为候选 Runner Snapshot 的动作。构建忠实使用该目录内容，不审查其 Git 状态、可信等级或与既有 Delivery Run 的兼容性；执行所选源码的安装器与 build backend 等同于授予该源码当前用户级代码执行权限，安装位置与清理承诺只约束符合本项目合同的源码。
_Avoid_: 自动跟随源码、来源审批、状态迁移、自动触发构建

**Runner Compatibility Check（Runner 兼容性检查）**:
源码安装器在激活候选 Snapshot 前发起的一次小型真实 Codex 调用，只确认当前调用链接受 Runner 使用的结构化输出 schema。结果不绑定 Codex 版本、不证明源码可信、不授权未来兼容，也不会在环境版本变化后自动重新检查；检查失败时保留原 Active Runner。
_Avoid_: Promotion Audit、Codex 版本门禁、源码验收、状态兼容检查、长期信任证明

**Runner Installation（Runner 安装）**:
用户在所选源码目录显式执行 `./install.sh`，并在 Runner Management Quiescence 中依次构建候选 Runner Snapshot、执行 Runner Compatibility Check、创建以新 Snapshot 为 current 且以可选旧 current 为 previous 的完整 Runner Generation，并在成功后原子切换 Active Runner 的动作。若存在活跃 Run Executor Session，安装立即拒绝且不修改受管状态。若候选 content identity 已经等于 Active current，安装器直接幂等成功：不重新执行 Compatibility Check、不创建 Snapshot 或 Generation，也不改变 previous。首次安装、切换 Git tag、拉取官方修改或构建本地未提交修改都使用同一动作；成功后固定只保留 Active Generation 引用的 current 与可选 previous，更早的 Generation 和 Snapshot 自动清理，失败时清理候选并保持旧 Generation 整体不变。切换后的清理失败只产生有界 warning，并由下一次管理动作重试，不回滚已经成功的 Active Runner。安装器只在 `~/.local/bin/agent-run` 缺失或仍是解析到受管 XDG root 的自有 symlink 时创建或替换入口；同名非受管路径使安装失败，不备份、不覆盖。用户级命令目录通过唯一边界标记的幂等受管块加入 shell PATH；安装器不安装或升级 Python、Git、`gh`、bubblewrap 等宿主软件，也不执行 `sudo`。安装器不检查或提示 Linux Executor Host Availability；公开生命周期动作在实际启动 Executor 前独立检查运行前置。安装不修改既有 Delivery Run 状态，也不创建兼容或迁移路径。
_Avoid_: 自动更新、重复 PATH 配置、系统包管理、`sudo`、源码热加载、可配置保留策略、无限快照历史、状态迁移、每-Run Runner 绑定

**Source Runner Installer（源码 Runner 安装器）**:
随每份源码树提供、只在显式执行 `./install.sh` 时运行的一次性安装程序。它把当前源码目录安装为候选 Snapshot；identity 已经等于 Active current 时直接幂等成功，否则调用 Runner Compatibility Check、创建完整 Runner Generation、切换 Active Runner，并只保留 current 与可选 previous Snapshot。它只检查 Installation Readiness，不查询、提示或启用 systemd、linger 或其他 Executor Host 能力。安装完成后不驻留、不参与 Controller 调用，也不形成独立包、版本或更新生命周期。v0.1 的公开分发入口是 Git clone 或切换到用户选择的 Git revision 后运行该安装器，不要求 PyPI 或 pipx。
_Avoid_: Runner Manager、常驻 Launcher、editable install、独立发布物、PyPI 前置条件

**Runner Installation Readiness（Runner 安装就绪度）**:
Source Runner Installer 为构建并激活候选 Snapshot 强制检查的本机条件，包括可用的受支持 CPython、venv/pip、源码与 build backend、受管路径写入与冲突边界、Runner Compatibility Check 以及 Runner 管理租约。它不包含 systemd、linger、目标 Repository、GitHub 登录或 lifecycle mutation 所需能力；`doctor` 可以只读报告它，但只有 Installer 在实际安装时作权威判定。
_Avoid_: Executor Host Availability、GitHub 执行资格、目标仓库、把 doctor 当作安装授权

**Runner Execution Readiness（Runner 执行就绪度）**:
一次公开 lifecycle mutation 在提交 Action 前按本次动作实际需要检查、并由 Executor 在真实宿主环境中通过启动握手最终确认的运行条件，包括有效 Active Runner、准确 Local Delivery Workspace/Delivery Task、Linux Executor Host Availability 以及本次 Agent、Git、GitHub、Publisher 或 sandbox 所需工具和认证。缺失时不得持久化新 Action或启动前台 fallback；`doctor` 只读报告，不替代动作时检查。`status` 与 `history` 不要求该就绪度。
_Avoid_: Runner Installation Readiness、一次全局永久判定、阻止只读查询、Installer systemd 检查

**Runner Management Lock（Runner 管理锁）**:
Source Runner Installer 与 Run Executor Session 共享的固定用户级非阻塞锁。Executor 从 Active Runner binding 确定前到 Session 退出或启动收口期间持共享 Runner Usage Lease；install、rollback 与 uninstall 必须取得独占 Runner Management Lease。多个 Executor 可以并行持有共享租约，任何共享或独占租约存在时竞争的独占管理动作立即失败且不修改状态；租约随持有进程退出由操作系统释放。uninstall 永不删除锁文件，避免替换持锁 inode 后出现第二把锁。
_Avoid_: 等待锁、每种动作一把锁、删除并重建锁文件、systemd 查询、自动停止活跃 Executor、启动交接空窗

**Runner Usage Lease（Runner 使用租约）**:
Run Executor Session 对当前 Active Runner 的主机级共享使用权。Active Runner 选择、binding claim 与租约从启动者到 Executor 的接管必须连续，不能让 Runner 管理动作在 systemd 已接受启动但 Executor 尚未握手的窗口穿插；Executor 或启动者退出时由操作系统自动释放。它只保护受管 Runner 内容，不扩大 Delivery Task 唯一性，也不保存 Run 业务状态。
_Avoid_: per-Run Snapshot 固定、引用计数、systemd unit 枚举、裸 PID、跨主机租约

**Runner Management Quiescence（Runner 管理静默状态）**:
本机不存在任何活跃 Run Executor Session 时形成的 Runner 管理前置状态。Runner Installation、Runner Rollback 与 Runner Uninstall 只在该状态下开始；任一活跃 Executor 存在时，管理动作立即拒绝，不等待、不停止 Delivery Run，也不改变 Active Runner。它保护的是当前用户共享的 Runner 生命周期，不扩大 Local Delivery Workspace 中 Delivery Task 的唯一性边界。
_Avoid_: 等待 Executor、自动 stop、运行中热更新、每-Run Snapshot 固定、跨工作区 Delivery Task 锁

**Runner Rollback（Runner 回退）**:
用户在任一包含源码安装器的目录显式执行 `./install.sh --rollback`，并在 Runner Management Quiescence 中创建交换 current 与 previous 的完整 Runner Generation、原子切换 Active Runner 的动作。若存在活跃 Run Executor Session，回退立即拒绝且不修改受管状态。它不重新构建、不调用 Codex、不判断 Runner 或 Delivery Run 状态兼容性，也不修改任何 Delivery Run；没有 previous 时明确失败且旧 Generation 整体不变。
_Avoid_: 状态迁移、兼容性检查、自动回退、下载历史版本、任意历史选择

**Runner Uninstall（Runner 卸载）**:
用户在任一包含源码安装器的目录显式执行 `./install.sh --uninstall`，并在 Runner Management Quiescence 中删除全部受管 Runner Snapshot、Runner Generation、候选残留、内部 `active` 入口、本机受管 `agent-run` symlink 以及安装器添加的 PATH 受管配置。若存在活跃 Run Executor Session，卸载立即拒绝且不修改受管状态。若用户已把公开入口替换为非受管路径，卸载保留该内容并以有界 operational error 报告清理未完成。它保留固定 Runner Management Lock、GitHub App 持久配置、全局 Run 定位状态和各目标仓库中的 `.agent-run` Delivery Run 数据；v0.1 不提供连带删除用户配置或运行数据的 purge 模式。
_Avoid_: 删除 Delivery Run、删除 GitHub App 配置、删除非受管 shell 配置、`--purge`

**Run 内部监督（In-Run Supervision）**:
Run Executor Session 在可自动判定的远端异步边界（例如 Required Checks、GitHub 事件最终一致性）内自行等待、退避重试和重新读取权威事实；维护者不为普通等待另行启动 watcher 或重复输入同一命令。GitHub 读取或对账的未知非零退出默认进入有界监督，Controller 只保存经脱敏、有界的错误证据，不从 `gh` stderr 推断网络、认证、权限、代理或其他具体原因。只有结构化远端事实已证明 Publisher intent、身份、head/base、检查、状态或关闭证据矛盾时，才转换为 Human Blocker；最终人工批准仍是独立授权边界。
_Avoid_: 维护者轮询 CI、常驻的第二套控制器、自动越过 Final Human Acceptance

**Run Executor Session（运行执行会话）**:
一个 Delivery Run 中连续推进所有无需新增人工授权工作的活动执行区间。它先且只先消费一个已接受的 Lifecycle Action，再从最新持久状态使用同一确定性 Driver 推进 Development、Acceptance、Publication 与 Run 内部监督，直到到达人类边界、Operator Stop、可恢复失败、监督超时或终态后结束。同一 Local Delivery Workspace 中的同一 Delivery Task 同一时刻最多有一个 Run Executor Session；同一工作区内不同 Delivery Task 可以并行，并由各自的 Managed Development Checkout 与任务控制边界隔离。agent-run 不为这种并行增加仓库级 Git 锁或跨 Worktree Git 协调层；Git 自身的并发规则仍由 Git 负责，agent-run 只处理自己发起的 Git 命令结果。Run Executor Session 不属于发起命令的终端，也不等同于整个 Delivery Run：终端离开不结束它，等待人工期间也不要求它继续存在。
_Avoid_: 终端拥有执行权、整个 Run 常驻进程、workspace 全局 daemon、把一次意图重复应用于自动循环、全局串行、同一本地任务多执行者、仓库级 Git 并发协调

**Executor Host（执行会话宿主）**:
在本机为准确 Local Delivery Workspace 与 Delivery Task 提供唯一 Run Executor Session 槽位的进程托管边界。它按准确 Delivery Run、执行代次与 Runner 身份启动、观察和回收 Executor，但不拥有 Delivery Run 业务状态、不决定 Controller 阶段，也不自动重放失败或结果未知的工作。每个 Delivery Task 的槽位彼此独立；Executor Host 不设置主机级并发数量上限、不建立跨任务队列，也不替用户分配整机 CPU 或内存。公开生命周期命令和 RunDriver 只依赖统一 Host 合同，不依赖具体操作系统的托管机制。
_Avoid_: 业务状态权威、常驻 workspace daemon、裸 PID 身份、自动 restart、主机级并发上限、跨任务资源调度

**Linux Executor Host Availability（Linux 执行宿主可用性）**:
Linux MVP 启动 Run Executor Session 的运行前置，表示当前用户的 systemd manager 可访问且能够创建用户级 transient service。它通常随正常登录会话提供，不要求安装、启用或常驻一个 agent-run service，也不要求开启 Logout Persistence。该能力不可用时，公开生命周期动作明确失败并说明环境缺口，不回退为绑定当前终端的第二套执行模式；`status` 与 `history` 等只读查询仍可使用。
_Avoid_: linger 前置条件、常驻 agent-run daemon、手工 enable 固定 service、静默前台回退、阻止只读查询

**Executor Launch Environment（Executor 启动环境）**:
每个 Run Executor Session 从启动该 Session 的 lifecycle 命令所在终端取得的用户环境快照，使项目 PATH、虚拟环境、代理、自定义 SDK 与本机工具在后台执行中保持一致。快照通过仅当前用户可读、大小有界、绑定准确 Action/generation 且只能消费一次的临时载体交接，不进入 Run state、Action history、systemd unit metadata 或日志；载体在握手、确定失败、冲突收口、Host 证明退出或不长于握手窗口的截止时间到达时删除，迟到的已关闭 generation 不能消费。agent-run 自有 identity、路径、控制 capability 与 Publisher authority 不由该环境提供，也不能被其覆盖；下一次新 Session 重新从发起终端取得快照。
_Avoid_: 完整环境持久化、固定 allowlist 猜测项目工具、实时同步终端变化、秘密写入 unit、项目环境覆盖控制面

**Logout Persistence（退出登录持续能力）**:
Executor Host 在维护者最后一个登录会话结束后仍允许当前 Run Executor Session 继续运行的可选宿主能力。它不同于 Action Completion Observation 离开或普通 Terminal 关闭，不是创建、推进或恢复 Delivery Run 的前置条件；缺少该能力只表示 Executor 不保证跨 logout 存活。它不承诺跨主机重启继续，也不授权自动重放任何 Agent 或外部写操作。
_Avoid_: 运行前置条件、Terminal Independence、重启恢复、自动 restart、Delivery Run 持久性

**Action Completion Observation（动作完成观察）**:
`stop` 与 `abandon` 在 Lifecycle Action 安全接受后，由当前命令对该 Action 直至准确目标状态的有界只读观察。观察只呈现少量结构化进展，可以随时离开；关闭终端或中断观察不会撤销 Action、停止 Executor、改变 Run 状态或转化为 Execution Failure。`status` 只返回一次当前 Delivery Progress View，其他公开生命周期命令也不进入持续观察。
_Avoid_: 通用 watch 命令、终端所有权、Worker PTY 代理、以 Ctrl-C 隐式停止 Run、第二份运行状态、无界日志

**Lifecycle Action（生命周期动作）**:
维护者通过公开生命周期命令向准确 Delivery Run 提交的一次性、可验证意图。Action 在当前 Run 状态允许时被接受，并由唯一 Run Executor Session 至多消费一次；`run`、`resume`、`approve`、`revise`、`requeue`、`stop` 与 `abandon` 共用这一入口和后续 Driver，不各自形成独立控制流程。活动 Executor 且没有未完成 Action 时再次收到普通 `run`，只返回现有执行状态而不创建新 Action；`resume`、`approve`、`revise` 与 `requeue` 只在准确对应边界接受，否则立即拒绝；`stop` 与 `abandon` 可在活动执行中请求收口。不被当前状态允许的 Action 不等待未来状态、不进入通用队列。完全匹配当前 unresolved Action 的 task、kind 与用户显式输入的重复原命令可以在新 Action 准入前附着并对账原 Action；它继续确认第一次接受时已解析并保存的语义 payload，即使其后用户级默认配置发生变化，也不重新解释第一次操作。原 Action 未收口时不产生第二个 Action 或历史，只有确认原 Action 已失败并持久收口后，同一次用户命令才可顺序提交 successor Action。其他相同或不同命令仍立即拒绝。
_Avoid_: 终端控制文件、重复消费、通用消息队列、过期授权、命令各自直接驱动 Engine、隐式授权、重复命令创建第二个 Action

**Action Admission Gate（动作准入门）**:
一个 Local Delivery Workspace 中每个 Delivery Task 对维护者 Lifecycle Action 的单槽位准入规则：同一时刻最多一个已接受但尚未完成的 Action，后来的新 Action 立即拒绝且不修改 Run、不等待、不排队。`run`、`resume`、`approve`、`revise` 与 `requeue` 在各自意图已按 Action identity 准确应用且 Run Executor Session 完成启动握手时释放准入；`stop` 与 `abandon` 在各自目标状态持久形成时释放准入。RunDriver 的自动工作不占用该门，只读查询与其他非 Delivery Run 生命周期管理也不经过该门。Action 中断或结果未知时，完全匹配的原命令可以附着并对账；只有先形成原 Action 的确定终态并释放单槽，才可顺序接受 successor，不能仅清除忙碌标记后接受下一项。
_Avoid_: 整个 Run Executor Session 锁、全仓库串行、Action 队列、优先级升级、过期授权、阻止 status/history、遗留 busy 标记

**Configuration Mutation（配置变更）**:
只修改后续工作所使用配置、而不启动、停止或推进 Delivery Run 的操作。用户级 Delivery Policy 与 GitHub 读取身份配置只影响之后适用的工作；Run-scoped Agent Profile Revision 只影响该 Delivery Run 之后新建的顶层 Thread，已经存在的 Thread 保持原绑定。Configuration Mutation 不属于 Lifecycle Action，不占用 Action Admission Gate；即使当前 Run Executor Session 正在执行，也可通过自身的有界原子写入保存。它不得借配置入口改变当前阶段、替换正在执行的 Agent 或取得 Publisher authority。
_Avoid_: 把配置命令排入 Action 槽位、用配置命令启动或停止 Run、热替换现有 Thread、通过配置绕过生命周期授权

**Task Control Record（任务控制记录）**:
一个 Local Delivery Workspace 中每个 Delivery Task 的有界、原子控制面，集中保存 Local Delivery Task Index 投影、当前或最近 Lifecycle Action、Executor ownership/generation、Runner binding 与启动握手。它是 Action currentness 与 Executor 所有权的权威，不复制 Delivery Run 业务状态、用户环境、完整 Action history 或日志；Delivery Run 对已接受 Action 及其结果只保存不参与 admission/routing 的有界审计投影。
_Avoid_: 第二份 Delivery Run state、Action 队列、无界事件日志、用户环境归档、以 history 决定 currentness

**Task Control Lock（任务控制锁）**:
每个 Delivery Task 独立使用的短时非阻塞互斥边界，只覆盖 Task Control Record 与相邻 Run receipt 的读取、验证和原子提交。它不得跨越 Agent、Git、GitHub、Publisher、进程启动握手、sleep 或外部监督，也不扩展为仓库级 Git 锁、主机级任务锁或整个 Run Executor Session 的长期租约。
_Avoid_: workspace 全局长锁、等待锁、跨外部副作用持锁、仓库 Git 协调、Runner Management Lock

**Task Control Reconciliation（任务控制对账）**:
Task Control Record 缺失、损坏或与 Host/Run receipt 不一致时，下一次公开生命周期命令在接受新 Action 前执行的确定性恢复边界。它只比较准确 Delivery Run 的 Action Application Receipt、Executor Host ownership 与可验证的执行代次；证据足够时原子补回控制记录并继续本次命令，证据不足时 fail closed 并给出可定位诊断，不猜测旧 Executor 已退出、不启动第二个 Agent。`status` 与 `history` 不执行该对账、不修复或写入状态。
_Avoid_: 只读查询隐式修复、删除 busy 标记冒充恢复、裸 PID 猜测、无法确认时启动新 Executor、重放业务 Action

**Action Application Receipt（动作应用凭据）**:
Delivery Run state 为一个已接受 Lifecycle Action 保存的最小、不可歧义事实，证明该 Action identity 对应的命令特定业务意图已经幂等应用。它只用于在 Run state 已提交而 Task Control 尚未收口的崩溃窗口中补完原 Action，不能授权 Publisher、代替 Action Receipt View、参与 Action history 展示或复制完整 Action payload；Task Control Record 只引用它，不重新解释业务状态。
_Avoid_: 用户回执、Action history、第二份业务状态、重放业务意图、Publisher authority

**Action Receipt（动作回执）**:
公开生命周期命令在返回前对本次 Lifecycle Action 给出的有界、准确结果。`run`、`resume`、`approve`、`revise` 与 `requeue` 的成功回执说明命令特定意图已按 Action identity 幂等应用到 Delivery Run，且准确 Run Executor Session 已完成启动握手或已被确认存在；它不证明 Agent 工作、当前自动区间或整个 Delivery Run 已完成。`stop` 与 `abandon` 的成功回执只在各自准确目标状态已经持久形成后给出；Action 接受后的中间进展属于可离开的 Action Completion Observation。启动或收口结果未知时不得伪装成功或直接创建第二个 Executor，须先依据 Host、Task Control 与 Run application receipt 对账。
_Avoid_: Delivery Run 完成声明、Agent 完成声明、仅凭进程启动请求、模糊 success、启动未知时盲目重试

**Action Receipt View（动作回执视图）**:
公开生命周期命令默认提供的面向操作者简洁叙事，与 Delivery Progress View 使用相同语言和信息层级。它回答准确 Repository 与 Parent、这次操作实际发生了什么、Agent Run 是否已经开始或原本就在运行、当前可理解状态，以及操作者是否需要行动和可使用的下一条 `status` 命令；`stop` 与 `abandon` 等待期间只展示少量结构化阶段变化，不输出 Worker 或宿主原始日志。成功输出保持简短，失败或结果未知时说明原因、保留了什么和如何恢复。默认视图不展示 Run ID、Action ID、SHA、digest、execution nonce、generation、Host slot、systemd unit、PID 或其他机器绑定，精确内部事实只进入 Machine Audit View 或诊断。
_Avoid_: 字段转储、内部状态码、SHA、哈希式身份、Executor Host 术语、把命令接受写成交付完成、无下一步的错误

**执行失败（Execution Failure）**:
Delivery Run 身份已经确定后，当前自动步骤因 Agent 进程、凭据、sandbox、timeout、signal 或其他不能安全继续的执行问题而形成的持久化可恢复暂停。Run Executor Session 必须先保留准确 Work Subject、Semantic Attempt、Thread、Managed Development Checkout 与 Failure Diagnostic Evidence，再停止并等待显式 `resume`；它不自动重试，不等同于 Action Completion Observation 离开。只有无法确定 Run 身份或无法安全持久化状态的故障才保留为命令级进程错误，并由 Host Diagnostic Log 提供最后的排查入口。
_Avoid_: 自动重试、监督超时、Human Blocker、无诊断证据的失败、丢弃未提交成果

**Failure Diagnostic Evidence（故障诊断证据）**:
Controller 在确定 Delivery Run 后为一次 Execution Failure 持久保存的有界、脱敏、可定位事实，使维护者无需依赖原终端即可判断故障发生在哪个 Work Subject、阶段与外部操作，并区分 Agent 中断、子进程失败、超时、信号、权限、持久化或宿主问题。Codex 的完整事件与对话由其自身 Session 记录负责，agent-run 只保留关联该 Invocation 与 Thread 所需的身份和失败摘要，不复制完整 Session、原始事件流或无界 stdout/stderr。`status` 展示可行动原因，Machine Audit View 保留更精确的诊断绑定。
_Avoid_: 完整 Codex Session 副本、无界 transcript、原始凭据、只有通用 error 字符串、依赖发起终端

**Host Diagnostic Log（宿主诊断日志）**:
Executor Host 交给操作系统日志设施保存的进程级诊断信息，用于在 Executor 尚未来得及写入 Failure Diagnostic Evidence 时追查启动、退出、signal、异常与宿主资源故障。它只接收 Executor 自身的有限诊断，不主动复制 Codex 原始事件流、完整子进程输出或 Delivery Run timeline；其保留时长和轮转由宿主策略决定，因此不是 Delivery Run 权威状态、恢复依据或用户历史。在 Linux 实现中，该职责由用户级 systemd journal 承担。
_Avoid_: 第二套 Run state、业务历史、完整 Agent 输出归档、无限保留、跨平台核心依赖

**Agent 执行所有权（Agent Execution Ownership）**:
一次 Agent 执行必须隶属于准确 Local Delivery Workspace、Delivery Task、Delivery Run、Run Executor Session 与当前执行代次，并由该任务唯一的 Executor 启动和回收。Executor 意外消失后，新 Executor 只有在确认旧执行者已不存在后才能开始新的 Agent 执行；无法确认时 fail closed。旧 Agent 执行被确认为已中断时，其 Invocation 先保存为 `execution_failed/session_interrupted` 并等待显式 `resume`，不自动重放；纯外部监督可继续原等待窗口，结果未知的 Publisher 写操作须先根据远端权威事实对账。本机进程身份只是所有权证据，不授权依据裸 PID 终止未经证明的进程，也不形成跨主机租约。
_Avoid_: 旧 Worker 存活时启动新 Worker、自动重放中断的 Agent、盲目重放未知的远端写入、裸 PID 猜测、自动杀死无法证明归属的进程、终端持有所有权

**Worker Lifecycle Isolation（Worker 生命周期隔离）**:
Codex Worker 保留项目开发所需的用户环境与 trusted-yolo 宿主能力，但机制性不可访问 agent-run 的 Task Control、Delivery Run mutation state、相关 locator、Runner 管理租约、lifecycle mutation 入口或 Publisher 自有凭证与控制 capability。它是 ADR 0001 宿主访问合同的窄控制面例外，不是通用文件系统 sandbox；Worker 可以读取普通项目事实和使用项目工具，但不能停止、恢复、批准、放弃或发布自己的 Delivery Run。用户主动注入任意第三方写凭证不可能按名称穷举撤销，不属于 agent-run 自有 Mutation Authority 保证。
_Avoid_: 只靠 Prompt 自律、Worker 自我批准、Publisher 凭证继承、禁止普通项目工具、宣称过滤任意第三方秘密

**公开生命周期命令（Public Lifecycle Command）**:
维护者用于创建或推进 Delivery Run 的稳定交互入口。`run` 是创建或恢复 Run 的唯一普通入口，也是不携带新增恢复或批准意图的自动入口：不存在未完成 Run 且 Parent 当前可执行时创建后继 Run；已有准确 Executor 正常运行且没有未完成 Action 时只返回现有状态，不提供单独的公开 `start` 或额外 `--new-run`。`run`、`resume`、`approve`、`revise`、`requeue`、`stop` 与 `abandon` 都只提交一次对应 Lifecycle Action，确保该任务唯一的 Run Executor Session 存在，并在取得准确 Action Receipt 后返回；它们不各自在调用进程内形成不同推进路径。`run`、`resume`、`approve`、`revise` 与 `requeue` 在命令意图准确应用并完成启动握手后返回；`stop` 与 `abandon` 默认通过可离开的 Action Completion Observation 等待各自目标状态后返回。`stop` 是可恢复的中止意图，停止后必须显式 `resume`；`abandon` 是不可恢复的终止、对账与受限收口。Development、Acceptance、Publication 与内部等待只是 Controller 阶段，不要求也不允许维护者把它们作为独立流程手工串接；监督超时可由同一 Parent 的显式 `run` 或 `resume` 恢复。
_Avoid_: 公开 `start`、额外 `--new-run`、把 `stop` 当作 `abandon`、手工反复执行内部阶段、把 `deliver` 当作公开工作流、以命令顺序替代 Controller 状态机

**Operator Stop（人工停止）**:
维护者直接结束当前 Run Executor Session 和准确 Agent 进程组、但保留 Delivery Run、Semantic Attempt、可恢复 Thread 与 Managed Development Checkout 以便后续恢复的明确 Lifecycle Action。Stop 可从任意终端提交；意图持久化后形成单调控制栅栏，不通知或等待 Agent 生成收尾输出，并禁止旧 Executor 开始新的 Agent、Git 或 Publisher 副作用。已经在途的单个 Publisher operation 只允许完成或进入对账，不能继续后续链式 mutation。成功 Stop 形成持久化 `operator_stopped` 边界并结束 Executor，不是执行故障或放弃 Run；后续必须显式 `resume`，Thread 已记录且仍可恢复时复用。当 Task Control 与 Executor Host 已能确认没有活动 Executor、没有在途 Stop，`stop` 只返回当前可理解状态和适用的下一步，不创建 Lifecycle Action、不修改 Delivery Run 或 Action history；无法确认是否仍有 Executor 时不得把它当作无操作成功。
_Avoid_: Execution Failure、Abandonment、新建 Semantic Attempt、丢弃 Agent 已落盘成果、等待 Agent 配合、中断未对账的远端写入、普通 run 隐式恢复、无声退出、无活动 Executor 时制造 Stop 历史

**No-progress Guard（无进展保护）**:
Run Executor Session 对一次自动步骤执行前后的确定性进展身份进行机械比较；若状态、当前 Work Subject、Generation、phase、Semantic Attempt、Invocation、Candidate、PR、准确 head、等待边界、cleanup 与下一步骤均未变化，则保存 `execution_failed/controller_no_progress` 并停止，而不以相同输入忙循环。时间戳、timeline、日志、展示字段与重试计数不构成进展；`waiting_checks`、`waiting_external` 与 `waiting_merge` 由 Run 内部监督负责，不属于无进展。
_Avoid_: 任意最大循环次数、把长时间 Agent 执行判为无进展、把外部等待判为无进展、依赖时间戳伪造进展

**监督截止时间（Supervision Deadline）**:
Run 内部监督按远端状态类别采用有限等待预算。预算内保持自动轮询与重对账；到期时保存最后的权威证据并进入监督超时暂停，而不是执行失败或假装通过。GitHub 只读状态或事件收敛的默认预算为 10 分钟，Required Checks 的默认预算为 45 分钟；其他类别须有自己的明确预算。
_Avoid_: 无限占用进程、把超时吞成 pass、把正常 pending 伪装成需要外部授权的 Human Blocker

**监督超时暂停（Supervision Timeout Pause）**:
可自动判定的远端异步状态在其监督截止时间内仍未收敛时，Run 保存最后的权威证据，当前 Run Executor Session 退出并等待维护者显式继续；它不要求维护者提供产品决策、权限、凭据或其他额外操作。`status` 与 `history` 在超时暂停时显示使用 Lifecycle Parent Selector 的 `agent-run resume <parent-issue>`；同一 Parent 的显式 `run` 也可重新读取权威状态并开始新的对应等待窗口。活跃 Executor 仍自行监督，维护者不应据此重复输入命令。超时 `resume` 不接受 Human Response 或新的 Agent Thread，且不会创建 Worker、PR 或 merge。
_Avoid_: execution_failed、Human Blocker、常驻 watcher、无界单次进程

**Ticket 关闭归属（Ticket Close Ownership）**:
Publisher 关闭 Primary Ticket 的可重建证明：它绑定 close-intent、PR 与 integrated SHA、intent 前的 Timeline watermark、baseline 后最新的关闭事件、Publisher actor 和 Issue 的 CLOSED 状态。GitHub Issue 与 Timeline 的时间字段可能异步收敛，不能要求其字符串完全相等；证据尚未收敛时属于 Run 内部监督，只有观察到更晚外部更新、重开、actor 或 binding 不匹配时才构成 Human Blocker。
_Avoid_: 仅凭 Issue 已关闭、时间字符串相等、把最终一致性延迟当作执行失败

**Publisher（发布器）**:
一个 Delivery Task 的唯一 Run Executor Session 中持有写凭证的受限模块，也是系统唯一的 Mutation Authority。它只接受 Controller 已校验且被 Change Job Contract 允许的动作，执行 Git、GitHub PR、评论、Issue 与合并写入；它不是独立服务，写凭证不会进入 Codex 子进程环境。
_Avoid_: Codex Worker、内容作者、智能审查者

**Mutation Authority（变更权限）**:
改变权威 Git 历史、metadata 或 GitHub 外部持久状态的排他权限；临时 checkout 的源码编辑和一次性 Git 数据不属于该权限。该权限只属于 Publisher，不因 Codex Worker 的建议、代码修改或完成声明而转移。
_Avoid_: Agent 自治、结构化输出、候选就绪

**Local Delivery Workspace（本地交付工作区）**:
维护者发起并恢复 Delivery Run 的准确本地 Git 仓库根目录，也是任务唯一性和 Run Executor Session 排他权的本地范围。两个独立本地仓库根目录由维护者自行协调，Controller 不扫描、不关联、不阻止它们对同一远端 Parent 各自启动 Run。Controller 为 Run 创建的 Managed Development Checkout 仍隶属原工作区，不是可重复启动该任务的新工作区。
_Avoid_: 主机级仓库注册表、跨 clone 锁、磁盘扫描、把 Managed Development Checkout 当作新的操作根

**Delivery Task（交付任务）**:
由准确 Repository 与 Parent Issue 共同标识的稳定远端交付对象。同一 Local Delivery Workspace 内，一个 Delivery Task 同时最多拥有一个未完成 Delivery Run；已完成或已放弃的 Run 保留为不可变历史，维护者可以显式创建一个后继 Run。不同 Repository 或同一 Repository 的不同 Parent Issue 是不同 Delivery Task，必须能够并行推进。
_Avoid_: 裸 `run_id`、单张 Child Ticket、整个 Repository、主机全局任务、跨独立本地仓库的唯一性承诺

**Delivery Run（交付运行）**:
由一次人工授权在一个 Local Delivery Workspace 中启动、覆盖一个 Delivery Task 的 Parent Issue 及其相关 Ticket、并以最终整体验收结束的交付范围。一个 Run 拥有独立身份和集成边界；同一工作区内同一 Delivery Task 的多个 Run 只能按时间先后存在，不能并行。普通 `run` 本身就是创建或恢复授权：没有未完成 Run 且 Parent 当前可执行时创建后继 Run，旧终态 Run 只保留为历史，不复活、不覆盖。
_Avoid_: 单张 Ticket、单次 Codex 执行、长期后台服务、同一 Parent 的并行 Run、复活或覆盖终态 Run、额外 `--new-run`

**Run 定位索引（Run Locator Index）**:
本机维护的最小 Run ID 到仓库根和 state 目录的定位记录。新登记同时保存 Repository 与 Parent number，作为 state 或 checkout 丢失时仍可核验的路由身份，不复制生命周期状态。查询兼容缺少这对字段的旧记录；可读取的 state、checkout 与路由身份必须一致，只有身份足以证明无关的记录才可排除。可能匹配但身份不足、状态失效或身份冲突时要求维护者显式消歧；查询成功和失败均不回填、删除或迁移索引与历史 Run，不扫描磁盘。索引最多保留最近 32 条，且不参与 Agent 编排、GitHub 状态、权限或生命周期 mutation。
_Avoid_: 全盘搜索、历史 state 迁移、跨仓库自动 mutation、Agent 执行日志、第二套 Run state

**Local Delivery Task Index（本地交付任务索引）**:
一个 Local Delivery Workspace 内维护的最小活动 Delivery Task 到唯一未完成 Run 及其 state 目录的指针。它只保存任务身份、Run 身份和位置，不复制生命周期状态、不参与 Agent 编排，也不跨独立本地仓库目录发现或阻止 Run。Run Executor Session 结束不删除指针中的未完成 Run；下次显式 `run` 继续它。指针若仍指向已完成或已放弃的 Run，新授权可将它替换为后继 Run；指向缺失、损坏或身份不一致的 Run 则 fail closed，不扫描其他目录、不猜测、不创建第二个 Run。
_Avoid_: 主机级索引、第二套 Run state、完整 Run 历史、磁盘扫描、远端任务注册表、静默覆盖冲突、复活终态 Run

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
一个 Delivery Run 独有、由 Controller 创建和维护的受管 Run Branch。已通过 Ticket Integration Gate（Fresh Acceptance 或 Deterministic Ticket Fallback）的 Ticket 变更先进入该临时集成分支，整个 Delivery Run 最终通过它接受人工整体验收后才进入默认分支。
_Avoid_: 默认分支、Ticket Branch、永久集成分支

**Parent-only Delivery（仅 Parent 交付）**:
当 Parent Issue 没有任何 Child Ticket 时使用的轻量完整交付：Publisher 创建与 Parent Issue 原生关联的唯一 Parent Branch 与直达默认分支的 Parent PR。它仍遵循共享的 Candidate-first、Fresh Acceptance、Required Checks 和 Published-Head Gate；每个预算窗口按有效 Run Policy Snapshot 的 Parent-only 轮数 `N` 允许最多 `N` 次 Development 与 `N` 次 Reviewer Invocation，并严格组成 `Development N → Reviewer N` 的完整配对，内置默认 `N=10`。每次 Reviewer 都调用 `code-review` skill；Reviewer 1 建立完整 Parent 基线，Reviewer 2–N 只额外获得上一轮 Acceptance Artifact 的完整原始内容、其 reviewed base/Candidate identity 和优先参考上轮问题的 Prompt 倾向，并自主决定审查范围；Reviewer N 仍失败时进入 Review Budget Checkpoint。Parent-only 直接面向默认分支且没有后继 Run Acceptance，因此不允许 Deterministic Ticket Fallback，也不允许任何未经当前 Reviewer 验收的新 Candidate 进入 Publication。`agent-run approve <parent-issue>` 只授予一次合并权限，Development–Acceptance Engine 随后重新核对当前 Parent Revision、验收记录、检查、默认分支与 PR head，并通过 Publisher 以普通 merge 合并。已合并但 closeout 响应丢失时，共享 Engine 只重试幂等审计评论和关闭，不得再次合并。
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
全部 Ticket Completion 后、首次创建 Run PR 前以及任何 Run Branch 或默认分支更新后执行的独立整体验收，也是 Delivery Run 的最终自动完成权威。它的对象是准确默认分支 head `D` 与准确 Run Branch head `R` 的预期合并结果：该结果必须可合并且满足完整 Parent Spec。正常 Attempt 使用全新 YOLO Reviewer Thread 和独立 Validation Checkout，要求调用 `code-review` skill，并针对 Parent Spec、Run Feedback Revisions、完整 Ticket Set、`D`、`R`、预期合并结果、累计 diff 和各 Ticket 集成证据进行检查；有效 Run Policy Snapshot 的 Run Repair 轮数 `N` 推导每个窗口最多 `N` 次 Run Development 与包含初始验收的 `N+1` 次 Run Reviewer Invocation，内置默认 `N=10`。Reviewer N+1 仍有可修复 Finding 时不自动开始下一次 Run Repair，而是进入 Review Budget Checkpoint。Reviewer 1 的 Prompt 要求建立完整 Parent 级基线；Reviewer 2–N+1 只额外收到上一轮 Acceptance Artifact 的完整原始内容及其 default base、Run head 与 expected merge tree identity，Prompt 建议优先参考上轮问题和当前 Repair，但 Reviewer 自主决定检查顺序、范围以及是否全量审核。维护者显式 `resume` 后开启有编号的新窗口，先将 Reviewer N+1 的 Findings 交给 Run Repair，再由新窗口的 Reviewer 1 验收修复后的新 Candidate。Human Blocker 的恢复可继续刚被阻塞的 Thread，普通 Finding 修复后的新 Candidate 仍由全新 Reviewer Thread 验收；每次都重新创建一次性 Validation Checkout 并核验边界。完整 Acceptance 输出不合法时最多进行两次同 Thread、只读 Output Repair。Run Reviewer 不复用任何 Development Thread 或 Ticket Reviewer Thread，重点检查跨 Ticket 交互、整体需求遗漏、局部实现累计偏离、集成回归以及所有 Deterministic Integration Record 中保留的 Reviewer Artifact 和后续 Development 事实；`D`、`R` 或有效需求 Revision 漂移都会使结论失效，只有通过后才允许生成或刷新 Run PR Narrative。
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
Delivery Run 独有并跨最终集成修复 Attempt 复用的持久 Development Codex Thread。它只接收 Run Acceptance Artifact、Run PR CI Failure Evidence 或默认分支合并冲突证据，以及 Parent Spec、完整 Run diff 和当前 Run Repair checkout，不复用任何 Ticket Development Thread；无法恢复时按 Development Thread 的相同显式 Resume / `--new-thread` 规则停止或恢复。
_Avoid_: Ticket Development Thread、Run Reviewer Thread、人工修复会话

**Integration-repair Worktree（集成修复工作区）**:
一个活跃 Repair Cycle 唯一、持久且可写的受管工作区。默认分支与 Run Branch 的预期合并发生冲突时，Controller 在此准备真实三方合并现场；Repair Codex 可读取 Git 状态、编辑和测试以解决语义冲突，但不得提交、push、rebase、创建 PR 或合并。它与稳定 Run Branch 及每次一次性、只读的 Validation Checkout 严格分离；同一 Cycle 的后续 Acceptance Finding 返回该工作区继续前进式修复，即使已集成 Job 被轮转为新的 Job/PR 也不新建开发工作区。每次 Candidate Run Acceptance 结束后，Controller 清理其 Validation Checkout，而不是丢弃仍可能继续修复的 Integration-repair Worktree。
_Avoid_: 在 Run Branch 直接修复、让 Reviewer 读取开发中目录、让 Codex 自行准备或发布 merge、为每个 Finding 新建 worktree

**Run Repair Job（运行修复任务）**:
Repair Cycle 中一次可发布修复使用 Change Job Contract 创建的 Development–Acceptance Engine 实例。它以当前 Run Branch 为 base、原始失败 Artifact、CI Failure Evidence 或冲突证据为修复输入；同一时刻最多一个 Run Repair Job 活跃。Job 未集成时，多轮 Development Attempt 复用该 Job 的 branch/PR；Job 已集成后，若同一 Cycle 的 Candidate revalidation 在新的 default head 上又产生 Finding，Controller 归档该 Job，并以新的 base、branch 和 PR 身份轮转出后继 Job，同时保留 Repair Cycle 的预算、Run Repair Thread 与 Integration-repair Worktree。
_Avoid_: Ticket Job、整个 Delivery Run 唯一 PR、独立修复流水线

**Repair Cycle（修复周期）**:
为解决同一准确 Run Acceptance、Run PR CI 或默认分支合并冲突问题而连续执行的有界修复工作；一个 Cycle 可依次包含多个 Run Repair Job，但任一时刻只允许一个活跃 Job。它在可修复 Finding、可修复 CI 失败或 Git conflict 出现时以零次修改开始；每当 Repair Codex 产出一个含实际交付树修改的新 repair Candidate，`code_modification_attempts` 加一，最多达到有效 Run Policy Snapshot 的 Run Repair 轮数 `N`，内置默认 `N=10`。冲突检测、worktree 准备、Reviewer 重跑、Required Checks 重试和无交付树修改的调查不计数。Candidate 或 repair PR 的 Required Check 只有同时得到 GitHub `FAILURE` 结论、准确绑定当前 PR head 的 completed Actions job，并由 job steps 证明失败仅发生在仓库 `pyproject.toml` 的 `tool.agent-run.required-checks.code-failure-steps` 所列 `workflow::name::step`，才被视为已明确且可由本次代码修复并返回同一 Cycle；pending、未知、缺少或矛盾的 job/step 事实、未配置 step、cancelled、runner 或暂时平台错误只进入 Controller 监督，不消耗预算。默认分支漂移以及已集成 Job 的 revalidation Finding 都不结束 Cycle 或重置预算；Candidate 被提升为正式 Run Acceptance、需要人工决定、预算耗尽，或 Run Branch/Parent/Graph/Ticket Completion 漂移时结束 Cycle。
_Avoid_: 用 Delivery Run 的历史累计修改数作为当前预算、把流程重试计作代码修改、base 漂移重置同一问题的预算

**Run Repair PR（运行修复拉取请求）**:
一个 Run Repair Job 独有并在该 Job 未集成前的多轮 Development Attempt 中稳定复用的 PR，其 head 是独立 run-repair branch，base 是 Run Branch。普通 Finding Repair 依次经过 Candidate、Candidate Run Acceptance、Publication、Required Checks 和 Published-Head Gate，以 squash merge 进入 Run Branch；默认分支与 Run Branch 的 Git 冲突则使用 Merge-resolution Candidate，repair PR 以普通 merge commit 进入 Run Branch，以保留已解决默认分支 head 的祖先关系。已合入的 PR 身份不可改写或复用；同一 Cycle 后续 revalidation Finding 由轮转后的新 Job/branch/PR 承载。
_Avoid_: Run PR、Ticket PR、将冲突解法 squash 到旧 Run head、直接推送 Run Branch

**Merge-resolution Candidate（合并解决候选）**:
默认分支 head `D` 与 Run Branch head `R` 发生 Git 冲突时，由 Publisher 根据 Integration-repair Worktree 的已解决文件树创建的受控双亲提交：其父提交准确为 `R` 与 `D`。它是 Candidate Run Acceptance 的候选对象；通过后，repair PR 只能以保留 `D` 祖先关系的普通 merge commit 进入 Run Branch。Publisher 必须验证实际 Run Branch 结果 tree 等于该 Candidate tree，且 `D` 仍为结果祖先，否则不得提升验收结论。
_Avoid_: Agent 自行提交 merge、squash resolution tree、隐式 rebase Run Branch、把冲突只当作文本补丁

**Run Acceptance Repair Loop（运行验收修复循环）**:
Run Acceptance 或已创建 Run PR 的 CI 产生可自动修复问题后，Controller 创建 Repair Cycle 与首个 Run Repair Job，并将原始 Acceptance Artifact 或 CI Failure Evidence 交给复用的 Run Repair Thread。Development–Acceptance Engine 产出 Candidate；Candidate Run Acceptance 以完整 Parent 范围检查准确 default head 与 Candidate 的预期合并结果。repair PR 的 Required Checks 与 Published-Head Gate 通过并合入 Run Branch 后，Controller 只有在 Candidate tree、repair base、实际 Run Branch tree、default head、Parent、Graph 与 Ticket Completion 均严格保持一致时，才将该 Candidate Acceptance 提升为正式 Run Acceptance。已受控合入后若仅 default head 前进，Controller 在同一 Cycle/worktree 重新预演和验收：新的 Finding 轮转新 Job/branch/PR 并继续消耗同一预算；其他 promotion 边界失配则废弃 Candidate，进入新的 Run Acceptance Generation。Git 冲突修复使用 Merge-resolution Candidate 与普通 merge，其他 repair 使用 squash merge。该循环持续到 promotion、需要人工决策或当前 Repair Cycle 耗尽策略规定的 `N` 次实际代码修复预算。
_Avoid_: 复用不等价的旧 Reviewer、直接修改 Run Branch、仅修复局部 Ticket diff、对同一已验收树无条件重跑 Reviewer

**Run Feedback Revision（运行反馈版本）**:
维护者通过 Final Revision Command 提交的 Run 级权威反馈及其稳定指纹。它进入 Run Repair Job 的 Effective Revision、Run Acceptance Artifact envelope 和后续 Run Acceptance 需求源，直到 Delivery Run 被批准或放弃；Controller 不总结、改写或静默丢弃该反馈。
_Avoid_: PR 普通评论、Parent Spec Revision、Controller 摘要

**Final Human Acceptance（最终人工验收）**:
Run Acceptance 与 Run PR Required Checks 全部通过后，Controller 首次向维护者请求的 Delivery Run 整体确认。维护者通过本地 `agent-run approve <parent-issue>` 显式批准；Controller 随后重新核对默认分支 live base、Run PR live head、Run Acceptance、Required Checks、Parent Spec Revision 与 Ticket Graph Revision，完全一致时 Publisher 才可使用普通 merge commit 和 `--match-head-commit` 合入默认分支。批准只绑定这一准确 head/base 与验收结果：同一 head/base 的 GitHub 收敛延迟由 Controller 自动监督，新 head 或 base 则使批准失效并要求新的最终人工验收。此前各 Ticket 的自动完成不触发逐票人工验收。
_Avoid_: Ticket 级确认、自动 merge 默认分支、Agent 语义范围判断

**Final Approval Command（最终批准命令）**:
维护者对一个已通过全部自动门禁的 Delivery Run 授予默认分支合并权限的本地显式命令 `agent-run approve <parent-issue>`。该授权只对命令执行时重新验证的 Run PR head、base 和有效 Revision 生效；同一事实集合的收敛延迟继续自动处理，新 head 或 base 则令授权失效并重新请求验收。
_Avoid_: GitHub Approve、标签触发、永久授权

**Final Revision Command（最终修改命令）**:
维护者通过 `agent-run revise <parent-issue> --message <feedback>` 提交的统一 Run 级人工恢复命令，既用于最终人工验收要求修改，也用于 Run Acceptance 返回 `human` 或累计修复预算耗尽。反馈原样形成新的 Run Feedback Revision、进入 Run Repair 输入，并显式开启按有效 Run Policy Snapshot 配置的 `N` 次 Run 修复预算窗口；修复后必须重新通过共享 Development–Acceptance Engine、全新 Run Acceptance、Run Publication 与 Run PR Required Checks。若反馈引起 Ticket Set 或依赖变化，Run 必须 fail closed 为 Unsupported Scope Change。
_Avoid_: Parent Spec 静默改写、直接修改 Run Branch、无限自动重试

**Final Abandon Command（最终放弃命令）**:
维护者通过 `agent-run abandon <parent-issue>` 永久终止尚未进入默认分支的 Delivery Run 的显式命令。Controller 执行 Run Abandonment Recovery、关闭或标记未合并的自动化 PR，并保留完整审计证据；暂时等待或普通修复不能使用该命令。若任一 Managed Development Checkout 仍含 tracked modifications 或 untracked files，普通 `abandon` 必须在任何外部写入或删除前拒绝执行并报告准确路径；只有维护者额外显式给出 `--discard-worktree` 才授权不可恢复地删除这些未提交成果后继续放弃。
_Avoid_: 暂停、单 Ticket 阻塞、默认分支回滚、将 `abandon` 默认解释为丢弃未提交成果

**Ticket Squash Merge（Ticket 压缩合并）**:
Publisher 在 Ticket PR 的 Published-Head Gate 通过后执行的固定合并方式。它使用 `--squash` 与 `--match-head-commit`，将 PR 公开分支上的 Publication 与 repair commits 作为一个新语义 commit 写入 Run Branch；首次推送前已压缩掉的本地 Candidate Commits 不会出现在 PR 页面。合并结果必须继续匹配 Ticket Integration Gate 绑定的 base、Publication tree 和语义标题。
_Avoid_: Rebase merge、普通 merge commit、发布前 Candidate 压缩

**Run Merge Commit（运行合并提交）**:
Run PR 通过最终人工整体验收后写入默认分支的普通 merge commit。其第二父历史保留 Run Branch 中各 Ticket squash commits，使系统既能按 Ticket 回滚，也能按整次 Delivery Run 回滚。
_Avoid_: Ticket Squash Merge、fast-forward、再次 squash 全部 Tickets

**Primary Ticket（主 Ticket）**:
一张 Ticket PR 唯一负责完成的 GitHub Issue，由 Ticket Job 身份确定，并通过 GitHub Development 原生关联与 PR 正文中的 `Primary Ticket` 引用共同表达。由于 PR 目标是非默认 Run Branch，Publisher 不依赖 closing keyword，而在合并后根据 Ticket Job 身份显式关闭该 Issue。
_Avoid_: Related Issue、Agent 猜测、一个 PR 关闭多张 Ticket

**Ticket Completion（Ticket 完成）**:
Ticket PR 通过 Fresh Acceptance 或 Deterministic Ticket Fallback、Required Checks 和 Published-Head Gate，并合入所属 Run Branch 后形成的完成状态。Publisher 随即显式关闭 Primary Ticket，并评论记录 Delivery Run、Ticket PR、采用的 Ticket Integration Gate 模式和进入 Run Branch 的 commit SHA，同时说明该变更尚未进入默认分支；该关闭动作使 GitHub `blockedBy` 依赖自然解除。Ticket Completion 只表示 Ticket 已进入临时集成边界，不表示完整 Parent 已最终验收。
_Avoid_: 合入默认分支、最终人工整体验收、closing keyword

**Ticket Completion Revision（Ticket 完成版本）**:
Run Acceptance 与 Run Repair Revision Snapshot 使用的确定性 Ticket 完成身份，包含 Ticket number、integrated SHA、Effective Revision、Ticket Integration Gate 模式及其绑定的 base/tree，并按 Ticket number 数值排序。完整 Acceptance Record 或 Deterministic Integration Record 继续保存在对应 Ticket Job 中，不嵌套复制到该版本身份。
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
Publisher 根据 Publication Artifact 保持候选 tree 不变，以 Run Branch 的有效 base 为父提交并使用 Agent 编写的语义 commit message 创建待发布提交。正常模式的发布权威输入是 Fresh Acceptance；Ticket 确定性兜底模式在 CI 尚未运行时由 Fallback Publication Receipt 只授权创建或更新 PR，不授权合入 Run Branch。Publisher 必须验证 Publication Commit 与对应记录绑定的 Candidate Commit tree 相同；随后 Hosted CI Gate 与 Published-Head Gate 将远端 PR head 绑定其准确 SHA。
_Avoid_: Candidate Commit、机械 checkpoint、远端 PR head

**Development Summary（开发摘要）**:
Development Codex 在 Development Attempt 结束时返回的普通、可读最后回复。它说明完成的工作、运行过的验证、已知阻塞及值得人类注意的后续影响，但不要求符合 JSON Schema，也不构成候选状态、测试通过或完成的权威证明。
_Avoid_: Publication Artifact、Controller Evidence、完成证明

**Publication Artifact（发布产物）**:
Candidate Commit 取得当前路径所需的发布权威后，由独立、只读的 Publication Codex 根据当前 Issue、最终 diff 与对应证据生成的小型结构化语义产物，提供语义 commit message、`type(scope): user-facing outcome` 格式的 PR title 和符合统一 PR Narrative 的完整正文。正常模式读取完整 Fresh Acceptance 证据；Ticket 确定性兜底模式读取 Fallback Publication Receipt，并准确说明语义 Review 预算已耗尽、当前 PR 等待 Required Checks，尚未取得 Ticket 集成权威。需求、最终方案、用户影响或验证证据变化时，Publication Codex 必须重新读取当前事实并更新产物；Publisher 只校验结构并执行写入，不改写 Agent 内容。
_Avoid_: Development Summary、独立发现工作流、验证记录

**PR Narrative（PR 语义正文）**:
Publication Codex 为 Ticket PR、Run Repair PR 与 Run PR 编写并保持准确的统一耐久说明，顶部身份分别使用 `Primary Ticket: #N`、`Delivery Run: <id>`、或 `Parent Spec: #N` 加 `Delivery Run: <id>`，正文均包含非空的 `What Problem This Solves`、`Why This Change Was Made`、`User Impact` 和 `Evidence`，Ticket PR 还可包含 Cross-Ticket Note。`Evidence` 记录独立 Fresh Acceptance、Run Acceptance，或明确标注为非语义验收的 Deterministic Ticket Fallback 真实背景，每条以“场景 → 实际操作或命令 → 可观察结果”表达；兜底 PR 只可读总结已使用 Reviewer Artifacts、最后一次 Reviewer Artifact 之后的 Development response 与代码 delta，不得自行断言 Findings 已关闭，也不得使用 Reviewer pass、CI pass 或语义验收措辞。Required Check 结果、Candidate、SHA、门禁和生命周期等机器事实只由 Publisher 的 Agent Run Status 评论呈现，不触发 CI 通过后的 PR Narrative 重写。UI、交互或可视输出变化时才加入 before/after 图片或视频，无内容的可选段落必须整个省略。
_Avoid_: Files changed 复述、通用 checklist、Agent 内部元数据

**Cross-Ticket Note（跨 Ticket 说明）**:
Development Codex 在 Ticket PR body 的可选 `## Cross-ticket notes` 段落中记录的非权威开发上下文，每项使用 `Affects #N: ...` 明确引用目标 Issue；没有相关信息时省略整个段落。后续 Development Codex 根据 Prompt 中的 GitHub 上下文约定，使用只读 GitHub 自主查找和阅读其认为相关的 Issue、PR 与 commit；Controller 不限制、解析或路由这些说明，它们也不修改 Issue、依赖图或 Acceptance Criteria。
_Avoid_: 正式需求增量、Issue 修改、验收门禁

**Publication Metadata（发布元数据）**:
Publisher 为每张已创建的开放自动化 PR 维护的一条 `Agent Run Status` 评论，原地更新 scope、Candidate/base、Ticket Integration Gate 模式、适用时的验收 lane、Required Checks 和 next action。它只是本地权威状态的简洁远端投影，不进入 Agent 编写的 PR 语义正文，也不发布完整 Acceptance Artifact、Acceptance Record、Fallback Publication Receipt、Deterministic Integration Record、repair input 或其他嵌套 JSON。
_Avoid_: PR Narrative、Agent 自述、重复验证说明

**Ticket Integration Gate（Ticket 集成就绪门禁）**:
Candidate Commit 进入 Run Branch、供下游 Ticket 使用前形成的有界证明。正常路径由 Fresh Acceptance 形成语义验收，并继续遵守目标 branch 实际配置的 Required Checks；Ticket Review 预算耗尽后，所有 Ticket 都可改用 Deterministic Ticket Fallback，由准确 PR head 按仓库实际 Required Checks 策略形成 Deterministic Integration Record：存在 Required Checks 时必须全部通过，未配置时明确记录 Required Checks Observation 的 `result=none` 并允许继续。它不是最终产品完成权威，也不重复证明完整 Parent、跨 Ticket 交互或全局低风险维护性质量，这些由 Run Acceptance 集中裁决。两种模式都必须绑定准确 base/tree/head，并在状态、Completion Revision 与最终 Run 输入中明确区分。
_Avoid_: Ticket 最终验收、把确定性兜底伪装成 Reviewer pass、缩小 Run Acceptance、隐藏门禁模式

**Deterministic Ticket Fallback（Ticket 确定性集成兜底）**:
每组配对的 Ticket Development 与 Review Budget Window 按有效 Run Policy Snapshot 的 Ticket Review 轮数 `N` 最多包含 `N+1` 次 Development Attempt 与 `N` 次 Reviewer Invocation，内置默认 `N=3`：初始 Development 1 后执行 Reviewer 1；每次失败都由下一次 Development 处理，并在仍有 Reviewer 名额时进入下一次 Reviewer；Reviewer N 仍失败且仍有普通 Development 名额时执行最后一次 Development N+1。Finding、Git Integrity 或可修复 Required Check 失败通常复用这一条时间线，实际发生的 Development 和 Reviewer 分别消耗对应统一预算；每个新 Candidate 只要仍有 Reviewer 名额就使用下一次 Reviewer，`N` 次名额已耗尽才不再审查。若其他失败已提前耗尽 `N+1` 次 Development，任一 Reviewer 的失败 Findings 都不得以未修复 Candidate 进入兜底，而是直接以 `modification_budget_exhausted` 等待人工 `resume`。只有最后一次 Reviewer Artifact 已作为后续 Development Brief 输入并产生新 Candidate、且 Review 名额确已耗尽时，才能进入 Deterministic Ticket Fallback；Controller 不判断其中哪些 Findings 已关闭。该 Candidate 不运行 Controller 本地代码 Validation、changed-surface 命令或 `git diff --check` 代码门禁；Controller 只保留 Git Integrity Check，并以 Fallback Publication Receipt 复用现有 Publication 阶段创建或更新普通 Ticket PR，不增加 provisional PR 类型或并行流水线。Controller 在准确 PR head 上读取 Required Checks Observation：`result=pass` 表示配置的必需检查全部通过；`result=none` 表示仓库没有配置必需检查，形成 Deterministic Integration Record 时不得声称运行过 CI，并允许 Published-Head Gate 合入 Run Branch。唯一预算例外是 Final CI-fix Allowance：`N+1` 次普通 Development 已耗尽后，任一已发布 Ticket PR 首次出现经 CI Failure Evidence 分类确认的可修复 Required Checks 失败时，可允许同一 Development Thread 额外执行一次 `attempt_kind=final_ci_fix`。修复后的新 Candidate 先通过 Git Integrity；仍有 Reviewer 名额时必须使用下一次 Fresh Acceptance，Reviewer 通过后再更新 PR 并运行准确新 head 的 CI；Reviewer 名额已耗尽时不创建额外 Reviewer，直接按 Fallback Publication Receipt 更新 PR 并运行 CI。Reviewer 失败、准确新 head 再次出现可修复 CI 失败或 Final CI-fix 后 Git Integrity 失败都等待人工 `resume`。其他 Git Integrity 或可修复 Required Check 失败按普通 Development Attempt 和 `N+1` 次预算处理；pending、未知、证据不完整或矛盾、未配置 code-failure step、cancelled、runner 与暂时平台错误只进入有界 Controller 监督，不启动 Agent 或消耗 Development/Review 预算。该路径适用于所有 Ticket，并将已使用 Reviewer Artifacts、最后一次 Reviewer Artifact 之后的 Development Summary 与代码 delta、Final CI-fix 使用情况及 Required Checks Observation 保留给 Run Acceptance，不产生或伪造 Fresh Acceptance pass。
_Avoid_: Controller 本地代码 Validation、changed-surface 配置、独立 validation-fix/Git-fix、多个 Final CI-fix、超过策略上限的 Reviewer、还有 Reviewer 名额却跳过审查、把未配置 CI 记为通过、伪造 Acceptance Artifact、伪造失败 Findings、把 Run Branch 集成当作默认分支交付

**Fallback Publication Receipt（兜底发布凭据）**:
Ticket Review 预算耗尽后，Controller 为准确 base、Candidate SHA/tree、Effective Revision、配对的 Development/Review Budget Window、已使用的 Reviewer Invocations 及其准确 Artifact/Reviewed Candidate、最后一次 Reviewer Artifact、随后 Development Attempt 的 Summary 与代码 delta、`final_ci_fix_used` 及适用时的原 CI 失败证据与修复 delta，以及 Publication 前适用的 Git Integrity 结果形成的不可变发布凭据。Controller 只记录这些原始事实，不判断 Findings 的 closure 状态。它只授权 Publisher 沿现有 Publication 阶段创建或更新普通 Ticket PR，以便 GitHub 对准确 head 运行 Required Checks；推送、live head 与合并阶段仍分别执行当时适用的 Git Integrity Check。它不构成 Ticket Integration Gate、Acceptance Record、测试通过证据或进入 Run Branch 的权限。任何新 Candidate、base/head 或 Revision 变化都使旧凭据失效并要求生成后继凭据。
_Avoid_: Deterministic Integration Record、Reviewer pass、PR 合并权限、provisional PR 类型、本地代码验证记录

**Deterministic Integration Record（确定性集成记录）**:
Deterministic Ticket Fallback 在 Fallback Publication Receipt 绑定的准确 PR head 上完成仓库 Required Checks 策略求值后形成的 Integration Record。它不可变地绑定 base、Candidate 与 Publication SHA/tree、Effective Revision、配对的 Development/Review Budget Window、已使用 Reviewer Invocations 的状态与 reviewed Candidate、最后一次 Reviewer Artifact、随后 Development Summary 与代码 delta、`final_ci_fix_used` 及适用时的原失败 head/evidence 与修复 delta、准确 PR head，以及该 head 唯一的 Required Checks Observation。Observation 的 `result=pass` 表示配置的必需检查全部通过，`result=none` 只表示仓库没有强制 CI，不构成测试运行或通过证据。任何新 Candidate、push、base/head、Required Checks 配置或 Revision 变化都使旧记录失去集成权威。它只授权当前 Ticket tree 在 Published-Head Gate 继续成立时进入 Run Branch，是 Run Acceptance 必须读取的待整体验证证据，不是语义 Acceptance Record。
_Avoid_: Fallback Publication Receipt、Acceptance Record、Reviewer pass、把 `result=none` 记为 CI pass、可跨 head 复用的 CI 摘要、最终完成证明

**Fresh Acceptance（独立验收）**:
Candidate Commit 创建后、生成 Publication Artifact 与 Publication Commit 前，由 Controller 按 Change Job Contract 启动全新 YOLO Reviewer Thread，在独立 Validation Checkout 中针对准确的 base SHA、Candidate Commit SHA、有效 Revision、需求源和验收标准执行实际使用与代码审查。Reviewer Prompt 要求调用 `code-review` skill。正常新一轮验收使用新的 Thread 和 checkout，不继承 Development Thread、旧 Reviewer Thread 或任何开发者自我判断；Reviewer 2+ 只额外获得紧邻上一轮 Acceptance Artifact 的完整原始内容及其 reviewed base/Candidate identity，由 Reviewer 自主决定如何利用。唯一例外是 Human Blocker resume：它复用刚刚被阻塞的 Reviewer Thread，但仍重新创建一次性 Validation Checkout 并重新核验。它可为验证构建和运行测试，但不得修复源码、测试、配置或 `.gitignore`，发现的问题必须进入 Acceptance Artifact。其 pass 是 Ticket Integration Gate 的正常模式；预算耗尽后的 Deterministic Ticket Fallback 是显式、非语义验收的另一模式，不能复用旧 Acceptance Record。
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
顶层 Codex 判断必须由人提供产品决策、外部权限、敏感凭据或不可替代外部操作才能继续时的最小结构化请求。Fresh/Run Acceptance 以一个或多个 `blocked` lane 的 evidence 表达；Development 使用 `result_kind: "human_blocker"`、`summary: null` 和非空 `human_blockers`，Publication 使用对应五字段 flat wire contract。Controller 只保存、展示与在 resume 时原样传回 blocker；维护者可不附加响应直接授权 Agent 重新核验，也可提供仅绑定当前 Job Generation 的不可变 Human Response，后者按顺序进入后续 Development 与 Fresh Acceptance，不修改 Issue、不触发 Requeue，也不与 Run Feedback Revision 混用。每个 response 最多 8 KiB；同一 Generation 的序列不按容量截断，替换 Generation 会从空序列开始，绝不向新 Generation 注入旧响应。任何当前顶层 Work Subject（包括 Ticket/Parent-only Change Job、Run Acceptance 与 Run Publication）产生 Human Blocker 都建立 Run-wide Operator Gate；恢复成功后当前 blocker 告警和全局门禁会清除，原始 blocker/response 尝试按顺序保留。
_Avoid_: Controller 诊断、subagent 事件、自动重试策略、笼统失败摘要

**Human Response（人工响应）**:
维护者通过 `agent-run resume <parent-issue> --message "..."` 为当前 Human Blocker 提供的原样、不可变
上下文。Controller 只做 trim 后非空和最多 8 KiB 两项机械输入边界，不判断内容正确性、不检测敏感信息，也不改写、过滤或摘要；通过边界后的文本原样按顺序绑定当前 Job Generation，进入后续 Development 与 Fresh Acceptance。它不适用于 `execution_failed`、不修改 Issue、不触发 Requeue，也不等同于 Run Feedback Revision。替换 Generation 从空序列开始。
_Avoid_: Run Feedback Revision、Issue 编辑、跨 generation 上下文

**Review Finding（审查发现）**:
Acceptance Artifact 的一个 lane 中可由 Development Codex 独立修复和验证的问题单元。它以 `问题：…；证据：…；必须修复：…；复验：…` 格式的非空字符串表达；所有 Finding 都要求修复，不存在建议型或非阻塞 Finding。人工产品决策、外部权限或不可替代操作进入该 lane 的 `blocked` evidence，不伪装成 Finding。
_Avoid_: 风格意见、无证据猜测、实现方案命令

**Acceptance Repair Loop（验收修复循环）**:
Fresh Acceptance 要求修改时，Controller 将原始 Acceptance Artifact 作为新的 Development Brief 输入，在同一 Change Job、working branch 与 Development Thread 上开始新的 Development Attempt。修复后必须重新自测、通过 Git 完整性检查并创建新 Candidate；Review 预算尚未耗尽时，由新的 Fresh Acceptance Reviewer Thread 验收。Ticket 的 Reviewer N 仍失败且仍有普通 Development 名额时，下一次 Development 处理其 Findings 后改走 Deterministic Ticket Fallback；若 `N+1` 次 Development 已因其他失败提前耗尽，则不得发布未修复 Candidate，直接以 `modification_budget_exhausted` 等待人工 `resume`。Run 的 Reviewer N+1 仍失败时进入 Review Budget Checkpoint；这里的 `N` 均来自对应层级的有效 Run Policy Snapshot。Reviewer 本身不修改代码。
_Avoid_: Reviewer 直接修复、创建新 Ticket Job、复用旧验收结论

**Published-Head Gate（已发布 Head 门禁）**:
Publisher 推送 Publication Commit 后，Controller 确定性验证目标 PR 的 live head、base、有效 Revision、Required Checks、mergeability 与正常路径的 Acceptance Record 或兜底路径的 Deterministic Integration Record 完全一致。Fallback Publication Receipt 只能授权推送 PR，不能单独通过本门禁。它不进行第二次 Codex 语义审查；任何 head、base 或 Revision 漂移都会使旧门禁证据失效，最终合并使用 `--match-head-commit` 绑定准确 SHA，并在完成 Ticket 前验证 integrated commit 的 parent、tree 和标题。若远端已合并但本地同步中断，恢复必须先对齐本地 Run Branch，再完成 Ticket。
_Avoid_: Fresh Acceptance、GitHub PR Exact-Head Review Loop、智能代码判断

**Git Integrity Check（Git 完整性检查）**:
Publisher 在创建、压缩、推送和合并候选时执行的确定性 Git 校验，包括 clean tree、预期 HEAD、base 绑定、压缩前后 tree equality、远端 lease 和 live head equality。它不运行仓库测试，也不判断实现是否正确。任何失败都不创建独立 Git-fix 阶段、专用 Agent 或独立预算，而是将原始失败证据以 `repair_source=git_integrity` 返回同一 Change Job 的现有 Development Thread，开始一次普通 Development Attempt；该 Agent 仍不得 commit、push、force-push、rebase 或 merge。Development 产出新 Candidate 后重新执行完整检查；普通 Development 预算耗尽则进入人工 Checkpoint，维护者可用 `resume` 开启新预算窗口后继续。失败检查本身不消耗 Development 或 Reviewer 预算，只有实际恢复的 Development Attempt 按普通规则计数；检查通过前不得继续 Publication、PR 更新、合并或 Ticket Completion。
_Avoid_: Hosted CI Gate、Fresh Acceptance、本地代码 Validation、独立 Git-fix 状态机、Git-fix 专用预算、授予 Development 发布权限

**Required Checks Observation（必需检查观察）**:
Controller 对一张 Published PR 的准确 head 读取并持久化的一次完整 Required Checks snapshot，绑定 PR 身份、head SHA、`none|pass|pending|unknown|fail` 汇总结果与实际 Check 明细。Ticket、Parent-only、Run Repair 与 Run PR 使用同一观察语义；新 publication head 使旧观察失效，但各自的等待、Repair、Approval 与 Merge 规则保持独立。
_Avoid_: 仅有汇总结果、旧 head 的观察、CI Failure Evidence、为满足字段形状合成空 Check

**CI Failure Evidence（CI 失败证据）**:
Required Checks Observation 为 `fail` 后，Controller 针对同一准确 PR head 取得的 completed Actions job 与 step 事实，用于判断失败是否可由代码修复并作为 Required-Checks Repair 输入。它不是所有结果通用的观察记录，`pending`、`unknown`、平台错误或缺少可归因 job/step 的失败都不构成 CI Failure Evidence。
_Avoid_: Required Checks Observation、pending evidence、汇总结果、未绑定 head 的失败日志

**Hosted CI Gate（托管 CI 门禁）**:
任何 Published PR 推送后由 GitHub Ruleset 或 branch protection 针对 live head SHA 声明的 Required Checks 自动测试门禁，适用于 Ticket PR、Run Repair PR 和 Run PR。Controller 直接读取适用于 PR base 的 GitHub 配置及准确 PR head 上的 Required Checks Observation，不把存在 workflow 文件本身当作必过门禁，普通非必需 Check 不参与自动门禁，Publisher 不得使用 Ruleset bypass 权限绕过 Required Checks。Ticket PR 存在 Required Checks 时必须全部通过，并复用 Run Repair 已有的 CI Failure Evidence 分类：只有 GitHub 已给出 `FAILURE`、completed Actions job 准确绑定当前 PR head，且 job steps 证明失败只发生在仓库 `pyproject.toml` 的 `tool.agent-run.required-checks.code-failure-steps` 所列 `workflow::name::step` 时，才保存 CI Failure Evidence 并以 `repair_source=required_checks` 返回现有 Ticket Development Thread。有效 Run Policy Snapshot 推导的 `N+1` 次普通 Development 尚未耗尽时按普通 Development Attempt 计数；已经耗尽时只可使用一次 Final CI-fix Allowance，额外 Development 标记 `attempt_kind=final_ci_fix`。Final CI-fix 新 Candidate 仍有 Reviewer 名额时必须执行下一次 Fresh Acceptance，没有名额时才不审查并直接重新发布；无论哪条路径，准确新 head 再次出现可修复 CI 失败即等待人工 `resume`。pending、未知、缺少或矛盾的 job/step 事实、未配置 code-failure step、cancelled、runner 与暂时平台错误只进入 Controller 的 Required Checks 有界监督，不启动 Agent，也不消耗 Development、Review 或 Final CI-fix 额度；监督到期进入 Supervision Timeout Pause，可由 `run` 或 `resume` 继续读取，但不创建新预算窗口。Ticket PR 的 Observation 为 `result=none` 时按仓库无强制 CI 处理，不触发 Development、Final CI-fix、暂停或人工 `resume`，也不宣称 CI 运行或通过。Ticket 新 Candidate 使旧 Acceptance、Fallback Publication Receipt、Deterministic Integration Record 与 Required Checks Observation 失效，并按剩余 Reviewer/Development/Final CI-fix 额度继续。Run Repair PR、Run PR 与 Parent-only PR 的失败仍按各自既有 Job Contract、CI Failure Evidence 分类、Repair Cycle 和 Controller 监督规则处理，不由本次 Ticket 优化改写。
_Avoid_: Development Codex 自测、Controller 本地测试执行器、Fresh Acceptance、独立 Ticket CI-fix 阶段、多个 Final CI-fix、超过策略上限的 Reviewer、还有 Reviewer 名额却跳过审查、把 `result=none` 伪装成 CI pass、把 Ticket 策略隐式扩展到 Run

**Acceptance Record（验收记录）**:
Development–Acceptance Engine 在独立验收后本地持久化的权威记录，将唯一一份 Acceptance Artifact 绑定到 acceptance scope、reviewed base、已验收 Candidate 或 Run head、对应 tree 或预期合并结果、有效 Revision 和 Reviewer 身份。Controller 从 Artifact 的三个 lane 推导通过、返工或人工阻塞；正常验收路径只有仍然 current 的三个 lane 全部 `pass` 可以授权 Publication。Ticket Review 预算耗尽后的 Fallback Publication Receipt 是只授权创建或更新 PR 的显式例外，不构成 Acceptance Record，也不授权合并。后续 Attempt 替换当前 Record，历史只按恢复需要有界保留，GitHub 只接收简洁的 Agent Run Status 投影。
_Avoid_: Publication Metadata、PR 语义正文、永久适用于整张 PR 的结论

**Delivery Progress View（交付进度视图）**:
`status` 与 `history` 默认提供的面向操作者 CLI 文本视图，采用分段摘要组织，而非前端面板、原始字段表或内部状态转储。`status` 只读返回一次当前视图，不创建、恢复、停止、持续观察或修复 Run。`status` 依次回答运行对象与状态、整体及当前轮次进度、总时长、当前 Agent 的模型与推理强度、当前 Findings、系统下一步与用户是否需要操作；Run-wide Operator Gate 生效时，显示准确 Ticket/Parent Issue 或 Run 对象、角色与阶段，并说明整个 Delivery Run 已暂停、其他独立 Ticket 尚未继续。Task Control Record 不可读取或无法与 Host 对账时，`status` 与 `history` 仍展示 Delivery Run 中可独立验证的进度和历史，但明确说明当前 Agent 是否运行暂时无法确认，且生命周期动作会在提交前先执行 Task Control Reconciliation；它们不得猜测执行状态或把故障控制事实写回。触发 blocker 的 Invocation 已结束，因此阻塞项显示“触发阻塞的 Agent”及其角色、模型、推理强度与本轮时长，不将其误写为当前仍在运行的 Agent。当前 blocker 原文完整展示。`history` 按设备本地时间叙述 Development、Review、Required Checks、集成、Human Blocker、Human Response、恢复与完成等关键里程碑，并在结尾汇总轮次和总时长；较长的历史 blocker 与 response 只做简单、明确标记的确定性截断，完整原文留在 Machine Audit View，不引入摘要 Agent 或新的语义处理。内部身份和完整审计事实不属于该视图。
_Avoid_: 前端面板、调试转储、机器审计接口、完整内部状态、仅对齐字段的运维表格

**Operator Action View（操作者动作视图）**:
Delivery Progress View 在 Run 需要维护者介入时采用的统一区块，固定说明准确类型、所在 Ticket/Parent Issue 或 Run 对象与阶段、原因、已保留成果、整个 Run 已暂停，以及唯一允许的下一步；Human Blocker 额外显示触发阻塞的 Agent 角色、模型、推理强度与本轮时长。Human Blocker、Review Budget Checkpoint、Execution Failure、Supervision Timeout Pause、Requeue Required 与最终批准继续保留各自真实语义，不因共享版式而被统称为 Human Blocker。
_Avoid_: 把所有暂停叫作 Human Blocker、只显示内部状态码、缺少恢复动作、声称其他 Ticket 仍在推进

**Run-wide Operator Gate（运行级人工门禁）**:
当前顶层 Work Subject（包括 Ticket/Parent-only Change Job、Run Acceptance 与 Run Publication）到达任何必须由维护者明确处理的状态时建立的 Delivery Run 全局暂停；它在 Controller 选择另一个 Ticket 或进入下一顶层阶段前生效，保留准确阻塞对象和现有成果，并只允许该门禁对应的恢复或终止动作。Human Blocker、Review Budget Checkpoint、Execution Failure、确定性外部矛盾及其他 Operator Action View 状态都建立该门禁；普通 Ticket DAG 依赖、triage 不可领取状态和仍在监督预算内的异步等待不是人工门禁，Controller 仍可选择其他符合条件的 Ticket。新规范 Run 不积累多个 Ticket 人工门禁；当前门禁解除且原 Work Subject 恢复或收口后，Controller 才选择下一个 Ticket 或顶层阶段。
_Avoid_: Ticket 局部人工门禁、门禁期间切换 frontier、把普通依赖等待当作人工门禁、积累多个待选 blocker

**Human Run Selector（人类运行选择器）**:
操作者为只读 `status`、`history` 与 Run 发现命令选择 Delivery Run 的面向用户身份。当前仓库仅有一个进行中 Run 时可省略选择参数；当前仓库内可用 `--parent <issue-number>` 选择 Parent，任意目录可用 `--repo <owner/name> --parent <issue-number>` 组成跨项目选择器，`runs` 可按当前仓库或显式 `--repo` 列出候选。若同一仓库与 Parent 仍匹配多个 Run 或多个本地 clone，Controller 不根据最近时间猜测，而是显示仓库工作目录、Parent、状态与开始时间并要求进一步消歧。完整不透明 Run ID 与显式 `--state-dir` 继续用于自动化、精确排障和歧义恢复，但不是普通用户需要记忆或默认展示的身份。
_Avoid_: 把 Parent Issue 当成全局唯一身份、全局 `latest` 猜测、要求用户记住哈希式 Run ID、模糊前缀匹配

**Lifecycle Parent Selector（生命周期 Parent 选择器）**:
操作者为当前仓库中的 mutation 命令选择 Delivery Run 的统一用户合同：`run`、`resume`、`requeue`、`approve`、`revise`、`stop`、`abandon` 与 Run-scoped `configure` 都以位置参数形式接收 Parent Issue number，并可用显式 repository identity 校验目标。`run` 可以在不存在未完成 Run 时创建后继 Run；其他命令只选择一个已经存在且满足该命令当前适用边界的准确 Run，不创建新 Run。尤其是 `resume` 只匹配唯一可恢复边界，`approve` 只匹配唯一最终批准门禁；其余命令也必须遵守各自状态合同。零匹配或多匹配都拒绝，不根据最近时间猜测。完整不透明 Run ID 只保留给自动化、精确排障和歧义恢复，不是普通操作者流程的必填身份。
_Avoid_: 各命令使用不同 Parent 语法、把 Parent Issue 当成跨仓库身份、忽略命令专属门禁、mutation 隐式创建 Run、最近 Run 猜测、要求普通用户记忆 Run ID

**Machine Audit View（机器审计视图）**:
`status --json`、`history --json` 与公开生命周期命令的 `--json` Action Receipt 提供的稳定机器可读视图，保留诊断、自动化和精确审计所需的内部身份、版本绑定、调用记录、Failure Diagnostic Evidence 与计数层级；Host Diagnostic Log 仍通过宿主诊断入口查看，不复制进该视图。
_Avoid_: 默认交付进度视图、面向用户的叙事摘要

**Operator Display Time（操作者显示时间）**:
Delivery Progress View 中的事件时间使用执行 `status` 或 `history` 的设备本地时区渲染，并明确标注所用时区；设备时区不可用时回退到 UTC 并明确提示。
_Avoid_: 直接展示持久化 UTC、无时区时间、根据仓库推断时区

**Delivery Policy（交付策略）**:
操作者拥有的语义轮次与按角色 Invocation Deadline 规则，来源优先级为内置默认、用户级默认和单次命令覆盖；仓库内容不能自行扩大操作者的 Agent 成本或运行时长。Ticket、Parent-only 与 Run 继续复用同一预算记录、门禁和恢复机制，但分别用与自身时间线匹配的轮次推导策略：Ticket Review 轮数 `N` 推导 Development 上限 `N+1`，前 `N` 组 Development/Review 配对后保留最后一次无 Review 的 Development Fallback；Run Repair 轮数 `N` 推导 Development 上限 `N` 与包含初始验收的 Reviewer 上限 `N+1`；Parent-only 轮数 `N` 推导 Development 与 Reviewer 都为 `N`。内置默认分别为 Ticket Review 3、Run Repair 10、Parent-only 10。Invocation Deadline 的内置默认是 Development 5 小时、Review 2 小时、Publication 1 小时，并允许用户级默认和单次命令按角色覆盖。
_Avoid_: 独立配置出无法闭环的 Development/Review 组合、仓库级资源预算、动态读取的运行中策略、Agent 自选预算

**Run Policy Snapshot（运行策略快照）**:
Delivery Run 创建或维护者在预算耗尽检查点显式开启新预算窗口时解析并保存的准确 Delivery Policy；新窗口可以采用当时的用户级默认和本次命令明确覆盖。普通 Execution Failure、Human Blocker 或监督超时后的 Resume 只继续原 Semantic Agent Attempt 或原等待窗口，必须复用原 Policy Snapshot，不得改变预算或 Invocation Deadline。已开始及历史预算窗口不因外部默认配置或后继窗口变化而改变。本策略与 Invocation Deadline 采用一次明确的规范状态协议切换：缺少 Policy Snapshot 的旧 Run 不迁移、不兼容推进，也不根据旧 Attempt 或当前默认值反推策略；只有按新协议创建的 Run 可以继续生命周期命令。
_Avoid_: 每次命令重新读取默认值、普通恢复中途改预算、追溯改写已消耗预算、推断或迁移旧 Run、仓库隐式覆盖

**Agent Invocation（Agent 调用）**:
Controller 对一次阶段级 Codex 调用的持久记录。Ticket、Parent-only 和 Run Repair 的
Development、Fresh Acceptance 与 Publication Invocation 都在首个 Output Attempt 前成为 active，
并绑定 Work Subject、Generation、输入指纹、机械 Currentness Boundary、当前 Semantic Agent Attempt 与实际 Thread Execution Binding；记录只保存输入指纹、有界边界事实、model、reasoning effort 与 Agent Profile Revision，
不保存 Prompt、transcript 或 Acceptance Artifact。`thread.started` 在进程运行中
立即保存。零退出但不符合完整阶段 contract 的输出可在同一 Thread 中最多修复两次；repair
checkout 只读，且不增加领域 Development Attempt、Reviewer Invocation 或 Publication Attempt。一个 Invocation 只有一个覆盖初始 Output Attempt 与其 Output Repair 的按角色总 Deadline；Development 默认五小时，Review 默认两小时，Publication 默认一小时，均可由 Run Policy Snapshot 中的角色覆盖值替代。successor Invocation 获得新的完整角色 Deadline，但仍可属于同一个 Semantic Agent Attempt。进程失败、缺失或
不匹配的 Thread 结束当前 Invocation；只有符合 Automatic Invocation Recovery 边界的异常才允许自动同 Thread 恢复，普通异常最多一次，准确容量不足可持续恢复，不自动创建替代 Thread。操作者可默认 Resume 原
Thread，或用 `--new-thread` 明确以标准阶段 Prompt 新开 Thread。
_Avoid_: Development Attempt、自动替代 Thread、领域 retry

**Semantic Agent Attempt（语义 Agent 尝试）**:
Development、Reviewer 或 Publication 角色针对一个准确 Work Subject、Job Generation、Currentness Boundary 与角色序号完成的一轮语义工作。它在首个 Codex 进程前分配，可跨越初始 Invocation、successor Invocation、Thread 替换与 Output Repair；执行失败、Human Blocker 或前台中断都不创建新 Attempt。Development 在分配时占用一次 Development Budget，Reviewer 在形成合法 Acceptance Artifact 时占用一次 Review Budget，Publication 在分配时计入一次 Publication Attempt；三者的 Resume 均不重复计数。已持久且尚未收口的 pending Attempt 优先于新 Attempt 的预算门禁：即使窗口计数已达上限，Controller 也必须先继续该 pending Attempt，不得将它拒绝为预算耗尽。只有当前 Attempt 已形成完整角色结果，或其 Currentness Boundary 失效并按规则收口后，才能分配后继 Attempt。
_Avoid_: Agent Invocation、Output Attempt、Codex 进程启动次数、Thread 序号

**Reviewer Invocation（审查调用）**:
Controller 为一个准确 Candidate tree 或 Run 预期合并结果启动、并成功形成合法 Acceptance Artifact 的一次顶层 Fresh Acceptance 或 Run Acceptance Agent Invocation。Reviewer 在该 Invocation 内自主读取项目、运行工具、派发 subagent，以及最多两次机械 Output Repair，都只计一次 Reviewer Invocation；新 Candidate 需要新 Reviewer Thread 且成功形成合法 Artifact 时才增加一次。进程、凭据、sandbox、timeout、signal、Thread 错配或最终输出仍不合法等执行失败不消耗 Review Budget，按 `execution_failed` 暂停且不会自动无限重试。Ticket 与 Run 分别按自己的 Review Budget Window 计数。
_Avoid_: Reviewer 内部工具调用、subagent 数量、Output Attempt、Development Attempt、Required Check 重跑

**Publication Attempt（发布尝试）**:
Publication Codex 针对一个准确、仍 current 的发布边界生成合法发布 Artifact 的一轮语义工作。一次 Publication Attempt 可以包含初始 Agent Invocation、进程失败后的 successor Invocation 与最多两次 Output Repair；这些过程只用于完成同一语义 Attempt，不因再次启动 Codex 进程而重复计数。
_Avoid_: Output Attempt、Codex 进程重试次数、Publication Context Fallback

**Publication Operation Retry（发布操作重试）**:
Controller 或 Publisher 在一次 Publication Attempt 内对 GitHub 读取收敛、带精确绑定的写入或结果对账进行的有界确定性重试。它按外部操作风险单独计数，不增加 Publication Attempt，也不被 Invocation Resume 清零或绕过。
_Avoid_: Publication Attempt、Output Repair、无界 GitHub 重试

**Agent Execution Profile（Agent 执行配置）**:
Delivery Run 为未来创建的各顶层 Codex Thread 保存的 model 与 reasoning effort 选择，可以来自命名预设或用户自定义值。它只约束 Controller 直接启动的顶层 Codex，不约束这些 Codex 自行派发的 subagent。
_Avoid_: Codex 用户全局默认、Subagent Profile、Acceptance Policy

**Agent Execution Preset（Agent 执行预设）**:
创建 Delivery Run 时可选的一组内置 Development、Review 与 Publication Agent Execution Profile。预设只提供初始值；选择时其准确 model、reasoning effort 与角色引用会被解析进 Agent Profile Revision，已有 Run 不随预设定义更新。
_Avoid_: 动态模型别名、Acceptance Policy、Codex 用户全局配置

**Agent Profile Revision（Agent 配置修订）**:
Delivery Run 当前 Development、Review 与 Publication 三组 Agent Execution Profile 的一个有序、不可变快照。用户修改配置会创建新的 Revision；既有 Thread 保留原绑定，后来创建的新 Thread 绑定修改时已生效的最新 Revision。
_Avoid_: Thread Execution Binding、Job Generation、Codex 用户全局配置版本

**Thread Execution Binding（线程执行绑定）**:
一个顶层 Codex Thread 创建时从当时有效的 Agent Profile Revision 解析并绑定的不可变角色、model 与 reasoning effort。同一 Thread 的后续 Invocation、Resume 与 Output Repair 始终使用该绑定；配置修改只影响后来创建的新 Thread。
_Avoid_: Invocation 级切换模型、运行中切换模型、Codex 用户全局默认

**Output Attempt（输出尝试）**:
一个 Agent Invocation 内的一次 `codex exec` 进程执行。初始输出是第一个 Output Attempt；仅当进程
零退出、Thread 身份正确而本地完整 contract 不合法时，Controller 才在同一 Thread、只读 checkout 中
最多追加两次机械 Output Repair。Repair 不产生新的 Invocation、不消耗领域 Development Attempt、Reviewer Invocation 或
Publication attempt，也不适用于进程、凭据、sandbox、timeout、signal 或 Thread 错配失败。
_Avoid_: Invocation Resume、智能重试、独立持久 journal

**Invocation Resume（调用恢复）**:
调用层的续接动作，其业务含义必须区分 Business Resume 与 Execution Recovery；复用同一 Thread 或生成 successor Invocation 本身不表示授权了新的预算窗口。公开 `resume` 保持单一入口，本次恢复意图绑定准入时的准确暂停原因、对象与授权，不能因后续状态变化被重解释；自动执行续接不具有解除业务阻塞或开启预算窗口的权限。以下保留公开 `resume` 的承载关系，不能据此把两种语义合并。
维护者以 `agent-run resume <parent-issue>` 为当前 `execution_failed` 或 Human Blocker Invocation 创建的
successor Invocation。若失败 Invocation 属于一次已分配且尚未收口的 Development Attempt、Reviewer Invocation 或 Publication Attempt，Resume 只继续该语义 Attempt，不再次占用角色预算或增加 Attempt 计数，也不得接受 Delivery Policy 覆盖；这一原则与是否复用原 Thread 无关。每次 Resume 都是新的显式人工授权，系统保留其次数与失败原因供 `status`/`history` 审计，但不为同一 Semantic Agent Attempt 另设 Resume Budget 或硬上限。维护者中断活跃前台调用只表示将其暂停为可恢复的 `execution_failed`，不表示放弃 Attempt 或授权清理 Managed Development Checkout。对 Ticket 的 `modification_budget_exhausted`，维护者显式执行 Resume 会同时创建新的 Ticket Review Budget Window 与 Ticket Development Budget Window，并将该新窗口的 `final_ci_fix_used` 初始化为 false；对 Run 或 Parent-only 的 `review_budget_exhausted`，Resume 创建对应的新 Review Budget Window 与代码修复预算窗口。只有这两类开启新预算窗口的 Resume 才能为新窗口解析当前用户级默认或接受本次命令的 Delivery Policy 覆盖；旧窗口用量与策略保持不变。它仍是同一 Job Generation、复用仍有效的 Thread、branch 与 PR，
但不把新 Attempt 伪装成旧窗口的额外轮次。若检查点保留了尚需修改代码的准确失败证据，successor Invocation 必须先把该证据交回原 Development Thread，产生新 Candidate 后才启动新窗口的 Reviewer 1；尤其是 Final CI-fix 后准确 PR head 的 Required Checks 再失败时，不得先审查未变化且已知 CI 失败的 Candidate。`--new-thread` 或无可恢复 Thread 时才以该阶段完整标准 Prompt
新开 Thread。Resume 成功与否不改变 Job Generation，且在 preflight 发现 Currentness Boundary 已 stale
时不启动 Codex，只进入 `requeue_required`。JSON 格式修复步骤因执行异常中断后，默认同 Thread Resume 只续接该次只读修复并继承已用次数，不重新开放开发任务。人工 Resume 不重置同一 Semantic Agent Attempt 已用的普通异常自动恢复额度；本次进程异常恢复不改变格式修复额度耗尽后的既有授权规则。上述恢复目标合同见 ADR 0011，尚待实现。
_Avoid_: Output Repair、Publisher/check 幂等恢复、隐式 Requeue

**Business Resume（业务恢复）**:
在明确的业务暂停点，由维护者补充所需回应或授予继续执行的许可；Human Blocker 恢复延续已获授权的工作，预算耗尽检查点的恢复则明确授权新的预算窗口。它不表示修复一个意外退出的 Codex 进程。
_Avoid_: Execution Recovery、把所有 Resume 都解释为新开预算窗口

**Execution Recovery（执行续接）**:
Codex 在已获授权的工作尚未完成时异常结束后，对原工作、Thread、权限与执行步骤的续接；它可以按规则自动进行，也可以由维护者触发，但不新增或重置业务预算、窗口与格式修复额度。业务暂停不会仅因底层 Codex 进程已经结束而变成执行异常。
_Avoid_: Business Resume、重新开发、预算续期、Output Repair

**Automatic Invocation Recovery（调用自动恢复）**:
Executor 仍存活且确认 Worker 异常结束后，为完成同一 Semantic Agent Attempt 而进行的同 Thread 续接：普通异常最多一次，准确模型容量不足可持续自动恢复且不消耗普通异常额度，不替代 Executor 消失后的人工恢复。续接保留原工作、权限、业务预算与格式修复次数，并获得与原调用等长的新执行时限；JSON 修复中断时只续接该次只读修复，不重开开发或增加格式修复额度。
_Avoid_: Output Repair、自动替代 Thread、Executor 自动重启、将容量恢复扩大为所有错误的无限重试

**Review Budget Checkpoint（审查预算检查点）**:
Run 或 Parent-only 已到达当前 Review Budget Window 的自动边界，且仍有明确失败证据时形成的 `ready-for-human` 暂停。Controller 保留当前 Candidate、准确 Findings、Thread、PR 与所有旧窗口用量，不自动启动下一次 Development 或 Reviewer。维护者显式执行 `agent-run resume <parent-issue>` 即表示允许继续：在 Currentness Boundary 仍有效时同时开启该层级新的 Review Budget Window 和新的代码修复预算窗口，先把未解决证据交回原 Development Thread 修复；历史用量不删除，只是新窗口从零开始。Ticket 用尽有效 Policy Snapshot 的 `N` 次 Reviewer Invocation 且最后 Findings 已由后续 Development 处理产生新 Candidate 时，直接进入 Deterministic Ticket Fallback，不进入本 Checkpoint。Reviewer Finding 或 Git Integrity 失败时已无普通 Development 名额、可修复 CI 失败时普通 Development 与 Final CI-fix 均不可用、Final CI-fix 后 Reviewer 失败，或该修复后的准确新 head 再次出现可修复 CI 失败，才以 `modification_budget_exhausted` 等待人工恢复；其他 CI 状态只进入有界监督。
_Avoid_: Ticket Review 预算耗尽、Human Blocker、自动续期、清零历史、Resume 后先重审未修改 Candidate

**Ticket Review Budget Window（Ticket 审查预算窗口）**:
同一 Ticket Job Generation 内一次明确授权的、最多 `N` 次 Reviewer Invocation 的审计单元；`N` 来自有效 Run Policy Snapshot，内置默认 `N=3`。初始 Development 后的 Reviewer 1，以及任何 Finding、Git Integrity 或可修复 Required Check 失败后产生新 Candidate 时仍可使用的 Reviewer 2 至 Reviewer N 消耗该窗口；Reviewer N 仍失败后的最终 Development、Review 名额耗尽后的 PR/Required Checks 兜底、Final CI-fix 与 Required Checks 等待都不增加 Reviewer 次数。任何新 Candidate 尚有名额时必须使用下一次 Reviewer；名额耗尽且最近 Reviewer Findings 已由后续 Development 处理时直接改走 Deterministic Ticket Fallback，不暂停为 `review_budget_exhausted`。若其他失败已提前耗尽 `N+1` 次 Development，Reviewer 失败后没有普通 Development 名额，则不得把该未修复 Candidate 兜底发布；Final CI-fix 不能转用于 Reviewer Finding 或 Git Integrity 失败。已发布 PR 的可修复 CI 失败通常返回普通 Development Attempt，并受统一 `N+1` 次 Development 预算约束；普通预算耗尽时可使用一次 Final CI-fix，修复后的新 Candidate 仍按剩余 Reviewer 名额决定是否 Fresh Acceptance。没有适用额度、Final CI-fix 后 Reviewer 失败或准确新 head 再次出现可修复 CI 失败时以 `modification_budget_exhausted` 等待 `resume`；其他 CI 状态只进入有界监督。恢复后同时创建有编号的新 Ticket Review 与 Development 窗口并重置 Final CI-fix Allowance。既有用量与证据继续保留而不被清零或覆盖。
_Avoid_: 两次 Repair Cycle、Reviewer 内部 subagent、Required Check 重跑、独立 validation-fix/Git-fix、多个 Final CI-fix、隐式续期

**Run Review Budget Window（Run 审查预算窗口）**:
同一 Run Acceptance Generation 内一次明确授权的、最多 `N+1` 次 Run Reviewer Invocation 与 `N` 次 Run Development 的审计单元；`N` 来自有效 Run Policy Snapshot 的 Run Repair 轮数，内置默认 `N=10`。初始 Run Reviewer 不依赖 Run Development；每次 Run Development 都必须属于一个包含后继 Reviewer 的完整 Run Repair Round。每个新 Candidate 或新的准确预期合并结果接受完整 Run Acceptance 时消耗一次；Reviewer 已形成合法 Acceptance Artifact 后即消耗额度，即使随后因 Currentness Boundary 漂移而废弃该结论。Reviewer 内部工作、Output Repair、执行失败、等待和未启动 Reviewer 的确定性 currentness 检查不计数。每次 Reviewer 都调用 `code-review` skill。Reviewer 1 建立完整基线；Reviewer 2–N+1 只额外获得上一轮 Acceptance Artifact 的完整原始内容及其 default base、Run head 与 expected merge tree identity，Prompt 建议优先参考上轮问题和当前 Repair，但 Reviewer 自主决定检查顺序、范围以及是否全量审核。Reviewer N+1 仍产生可修复 Finding 时进入 Review Budget Checkpoint，不使用 Ticket 的确定性集成兜底；维护者显式 `resume` 可在 Currentness Boundary 仍有效时创建有编号的新窗口，先修复 Reviewer N+1 的 Findings，再由新窗口 Reviewer 1 验收；所有旧窗口的用量、Findings 与修复历史继续保留。该 Prompt 优化不改变 Run Acceptance、CI Failure Evidence、Repair Cycle、Publication 或合并状态机。Parent-only 复用相同的预算记录、门禁、Artifact 和 Resume 机制，只使用等量 Development/Reviewer 的配对推导策略，而不复用本窗口的 `N+1` Reviewer 拓扑。
_Avoid_: Ticket Review Budget Window、把 `N` 次 Development 与 `N+1` 次 Reviewer 混成一个计数、Reviewer N+1 失败后自动 Repair、隐式额外 Reviewer

**Ticket Development Budget（Ticket 开发预算）**:
一个 Ticket Job 的每个 Ticket Development Budget Window 对初始开发以及 Finding、Git Integrity 和可修复 Required Check 失败后实际启动的普通 Development Attempt 提供统一 `N+1` 次上限；`N` 是有效 Run Policy Snapshot 的 Ticket Review 轮数，内置默认 `N=3`。等待 CI、失败检查本身、重复读取状态、基础设施或未知 CI 状态以及对同一未变化 SHA 重新检查不消耗预算，只有实际启动并允许修改代码的 Development Attempt 才计数；不存在 validation-fix 或 Git-fix 专用额度。普通 `N+1` 次预算耗尽后，任一已发布 Ticket PR 首次出现可修复 Required Checks 失败可使用一次 Final CI-fix Allowance；它仍复用 Development Thread，以 `attempt_kind=final_ci_fix` 独立记录，但不增加 Reviewer 额度。新 Candidate 尚有 Reviewer 名额时必须审查，没有名额时才直接重新发布并运行 CI。没有适用 Final CI-fix、额外修复后的 Reviewer 失败或准确新 head 再次出现可修复 CI 失败时，Ticket 转为 `ready-for-human`，保留 Candidate、PR、findings、失败证据与历史，并建立 Run-wide Operator Gate；不可归因于代码的 CI 状态只走有界监督。只有维护者显式 Resume 才能创建有编号的新 Review 与 Development 窗口并重置 Final CI-fix Allowance；门禁解除并收口该 Ticket 后，Controller 才能选择其他任务。
_Avoid_: CI 等待次数、同一 SHA 重复审查、无限重试

**Ticket Development Budget Window（Ticket 开发预算窗口）**:
同一 Ticket Job Generation 内一次明确授权的、最多 `N+1` 次普通 Development Attempt 加一次条件式 Final CI-fix 的审计单元；`N` 是有效 Run Policy Snapshot 的 Ticket Review 轮数，内置默认 `N=3`，初始 Development 1 计入普通预算。它由持久的窗口编号、普通 Development 消耗次数和 `final_ci_fix_used` 表达；Final CI-fix 在 `N+1` 次普通 Development 耗尽后任一已发布 Ticket PR 首次出现经 CI Failure Evidence 分类确认的可修复 Required Checks 失败时可用，不能转给 Finding、Git Integrity、基础设施或其他失败。它不增加 Reviewer 额度：修复后的新 Candidate 有剩余 Reviewer 就使用，没有才跳过。新的窗口只能由维护者显式执行 `agent-run resume <parent-issue>` 在 `modification_budget_exhausted` 边界创建，并复用仍 current 的 Job、PR、branch、Candidate、findings 和历史；Currentness Boundary 已 stale 时只允许 `requeue` 创建新 Generation。
_Avoid_: Job Generation、CI 等待窗口、隐式自动续期

**Ticket Resume Command（Ticket 恢复命令）**:
维护者通过 `agent-run resume <parent-issue>` 恢复当前 failed 或 Human Blocker Invocation；在
`modification_budget_exhausted` 边界，该显式命令会同时新建有编号的 Ticket Review Budget Window 与 Ticket Development Budget Window，并重置该新窗口的 Final CI-fix Allowance。Ticket 用尽有效 Run Policy Snapshot 的 `N` 次 Reviewer Invocation 本身不产生 `review_budget_exhausted`；最后 Findings 已由后续 Development 处理出新 Candidate 时自动进入 Deterministic Ticket Fallback，没有普通 Development 名额处理 Findings 时则产生 `modification_budget_exhausted`。
若该边界保存了 Reviewer Finding、Git Integrity 或 Required Checks 失败证据，新窗口先启动普通 Development Attempt，并将准确原始证据交回原 Development Thread；只有产出新 Candidate 后才由新窗口 Reviewer 1 建立基线。Final CI-fix 后准确新 head CI 再失败也遵循此顺序，不审查未修改的失败 Candidate。
若 preflight
发现 Ticket Content Revision、Parent Revision 或适用 base 已变化，Controller 不启动 Codex，也不
复用原 Ticket Job、Ticket Branch 或 Development Thread；它停在 `requeue_required`，维护者需执行
`agent-run requeue <parent-issue>` 创建新的 generation。内容未变化时 Resume 仍可复用当前 Invocation 的
Thread；它不重置既有窗口的消耗记录。
_Avoid_: 隐式 Requeue、无内容变化重试、Run Feedback Revision

**Progress Exhaustion（推进耗尽）**:
Delivery Run 在没有 Run-wide Operator Gate 的前提下，已不存在可激活的 Ticket Job，但 Ticket Set 尚未全部完成的状态。它只汇总普通 DAG dependency、triage 不可领取或其他不要求维护者立即处理却使 frontier 为空的事实；任何 Ticket 已进入 Human Blocker、Review Budget Checkpoint、Execution Failure 或其他 Operator Action View 状态时，Controller 必须立即建立 Run-wide Operator Gate，不能先继续独立 Ticket，也不能把该门禁延迟到 Progress Exhaustion。
_Avoid_: Run-wide Operator Gate、单 Ticket 人工门禁、最终整体验收

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
