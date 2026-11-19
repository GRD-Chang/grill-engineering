# 执行宿主与生命周期

进程托管、动作准入、停止和观察各自的责任与权威。CLI 的操作路径见[运行说明](../agent-run.md#命令)，端口和模块归属见[当前架构](../architecture.md#模块责任)。

[按任务查阅术语与合同](../../GLOSSARY-MAP.md)

## 执行会话与宿主

**Run Executor Session（运行执行会话）**:
一个 Delivery Run 中连续推进已获授权自动工作的活动执行区间。它由已接受的 Lifecycle Action 启动，覆盖 Development、Acceptance、Publication 与 Run 内部监督，期间可接受局部 Ticket Resume；单 Ticket 的人工门禁、失败或监督超时不结束仍有独立工作的会话。整个 Run 已无自动工作、出现全局门禁或到达终态时，会话结束。同一 Delivery Task 同时最多有一个 Run Executor Session，不同 Delivery Task 可以并行。会话不等同于整个 Delivery Run；后台模式下不属于发起命令的终端，终端离开不结束它，所有剩余工作均等待人工时不要求它继续存在。运行中定向恢复及局部退出边界的需求修订见 [ADR 0019](../adr/0019-ticket-work-concurrency-and-supervision.md)。
_Avoid_: 终端拥有执行权、整个 Run 常驻进程、workspace 全局 daemon、把一次意图重复应用于自动循环、全局串行、同一本地任务多执行者、仓库级 Git 并发协调

### Executor Host（执行会话宿主）

它按准确 Delivery Run、执行代次与 Runner 身份启动、观察和回收 Executor，但不拥有 Delivery Run 业务状态、不决定 Controller 阶段，也不自动重放失败或结果未知的工作。每个 Delivery Task 的槽位彼此独立；Executor Host 不设置主机级并发数量上限、不建立跨任务队列，也不替用户分配整机 CPU 或内存。公开生命周期命令通过统一 Host 合同推进，各宿主负责自己的启动与退出证明。
_Avoid_: 业务状态权威、常驻 workspace daemon、裸 PID 身份、自动 restart、主机级并发上限、跨任务资源调度

**Linux Executor Host Availability（Linux 执行宿主可用性）**:
后台模式启动 Run Executor Session 的运行前置，表示当前用户的 systemd manager 可访问且能够创建用户级 transient service。它通常随正常登录会话提供，不要求安装、启用或常驻一个 agent-run service，也不要求开启 Logout Persistence。该能力不可用时，公开生命周期动作明确失败并说明环境缺口，不静默回退前台；显式前台候选共用原 Core/Executor，取消、退出与恢复仍待验收，见[运行模式](../agent-run.md#运行模式与前台候选验收)；`status` 与 `history` 等只读查询仍可使用。

启用通知的 Session 由宿主在退出后执行一次有界观察，核对准确 Run、Action、Generation、Runner 与异常主进程退出事实，再复用通知 outbox 自动尝试告警。观察不拥有业务写入或恢复权限；正常退出、Stop、等待批准、离开观察窗口和已替换代次不误报。无每 Run 常驻轮询，通知关闭不注册观察。环境私有副本、时间边界与外部不可用限制见[通知合同](../notifications.md#executor-意外退出)。
_Avoid_: linger 前置条件、常驻 agent-run daemon、手工 enable 固定 service、静默前台回退、阻止只读查询

**Executor Launch Environment（Executor 启动环境）**:
每个 Run Executor Session 从启动该 Session 的 lifecycle 命令所在终端取得的用户环境快照，使项目 PATH、虚拟环境、代理、自定义 SDK 与本机工具在后台执行中保持一致。快照通过仅当前用户可读、大小有界、绑定准确 Action/generation 且只能消费一次的临时载体交接，不进入 Run state、Action history、systemd unit metadata 或日志；载体在握手、确定失败、冲突收口、Host 证明退出或不长于握手窗口的截止时间到达时删除，迟到的已关闭 generation 不能消费。agent-run 自有 identity、路径、控制 capability 与 Publisher authority 不由该环境提供，也不能被其覆盖；下一次新 Session 重新从发起终端取得快照。
_Avoid_: 完整环境持久化、固定 allowlist 猜测项目工具、实时同步终端变化、秘密写入 unit、项目环境覆盖控制面

**Logout Persistence（退出登录持续能力）**:
Executor Host 在维护者最后一个登录会话结束后仍允许当前 Run Executor Session 继续运行的可选宿主能力。它不同于 Action Completion Observation 离开或普通 Terminal 关闭，不是创建、推进或恢复 Delivery Run 的前置条件；缺少该能力只表示 Executor 不保证跨 logout 存活。它不承诺跨主机重启继续，也不授权自动重放任何 Agent 或外部写操作。
_Avoid_: 运行前置条件、Terminal Independence、重启恢复、自动 restart、Delivery Run 持久性

**Agent 执行所有权（Agent Execution Ownership）**:
一次 Agent 执行必须隶属于准确 Local Delivery Workspace、Delivery Task、Delivery Run、Run Executor Session 与当前执行代次，并由该任务唯一的 Executor 启动和回收。Executor 意外消失后，新 Executor 只有在确认旧执行者已不存在后才能开始新的 Agent 执行；无法确认时 fail closed。因 Executor 意外消失而中断的旧 Agent 执行被确认后，其 Invocation 先保存为 `execution_failed/session_interrupted` 并等待显式 `resume`，不自动重放；纯外部监督可继续原等待窗口，结果未知的 Publisher 写操作须先根据远端权威事实对账。本机进程身份只是所有权证据，不授权依据裸 PID 终止未经证明的进程，也不形成跨主机租约。
_Avoid_: 旧 Worker 存活时启动新 Worker、自动重放中断的 Agent、盲目重放未知的远端写入、裸 PID 猜测、自动杀死无法证明归属的进程、终端持有所有权

**Worker Lifecycle Isolation（Worker 生命周期隔离）**:
Codex Worker 保留项目开发所需的用户环境与 trusted-yolo 宿主能力，但机制性不可访问 agent-run 的 Task Control、Delivery Run mutation state、相关 locator、Runner 管理租约、lifecycle mutation 入口或 Publisher 自有凭证与控制 capability。它是 ADR 0001 宿主访问合同的窄控制面例外，不是通用文件系统 sandbox；Worker 可以读取普通项目事实和使用项目工具，但不能停止、恢复、批准、放弃或发布自己的 Delivery Run。用户主动注入任意第三方写凭证不可能按名称穷举撤销，不属于 agent-run 自有 Mutation Authority 保证。
_Avoid_: 只靠 Prompt 自律、Worker 自我批准、Publisher 凭证继承、禁止普通项目工具、宣称过滤任意第三方秘密

## 动作与控制对账

**Action Completion Observation（动作完成观察）**:
`stop`、`abandon`、`approve` 及恢复原最终批准操作的 `resume` 在 Lifecycle Action 安全接受后，由当前命令对该 Action 直至准确完成或无法继续边界的有界只读观察。后台观察只呈现少量结构化进展，可以随时离开；关闭终端或中断后台观察不会撤销 Action、停止 Executor、改变 Run 状态或转化为 Execution Failure。前台候选的正常取消合同是请求停止并收尾，最终批准操作仍不可取消；实现验收限制见[运行模式](../agent-run.md#运行模式与前台候选验收)。`status` 只返回一次当前 Delivery Progress View，其他公开生命周期命令也不进入持续观察。
_Avoid_: 通用 watch 命令、终端所有权、Worker PTY 代理、以后台观察的 Ctrl-C 隐式停止 Run、第二份运行状态、无界日志

### Lifecycle Action（生命周期动作）

Action 在当前 Run 状态允许时被接受，并由唯一 Run Executor Session 至多消费一次；`run`、`resume`、`approve`、`revise`、`requeue`、`stop` 与 `abandon` 共用这一入口和后续 Driver，不各自形成独立控制流程。活动 Executor 且没有未完成 Action 时再次收到普通 `run`，只返回现有执行状态而不创建新 Action；`resume`、`approve`、`revise` 与 `requeue` 只在准确对应边界接受，否则立即拒绝；`stop` 与 `abandon` 可在没有未完成 Action 的活动执行中请求收口；最终批准操作执行期间不可取消，恢复该操作时同样适用。不被当前状态允许的 Action 不等待未来状态、不进入通用队列。完全匹配当前 unresolved Action 的 task、kind 与用户显式输入的重复原命令可以在新 Action 准入前附着并对账原 Action；它继续确认第一次接受时已解析并保存的语义 payload，即使其后用户级默认配置发生变化，也不重新解释第一次操作。原 Action 未收口时不产生第二个 Action 或历史，只有确认原 Action 已失败并持久收口后，同一次用户命令才可顺序提交 successor Action。恢复原最终批准操作的 `resume` 是有限例外：可在新动作准入前对账原最终批准操作的未完成 Action（approve 或恢复该操作的 resume），但仍须确认旧执行已结束并形成原 Action 的确定结果，才能继续未完成部分。其他相同或不同命令仍立即拒绝。
已有 Run 的 `resume`、`approve`、`revise`、`requeue` 在执行准备时可用已接受 Action 的冻结 payload Run ID 从受管状态定位准确对象；请求目标不代表执行权。核验规范状态、Repository/Parent 归属、当前 Action/Executor generation 与非空绑定一致性后，才正式绑定并通过命令原有业务检查应用意图。绑定尚未产生 Application Receipt 时重入仍须应用命令；准确 Receipt 已保存时只对账并继续后续工作，不重复意图。该定位不用于只读查询、通用回执或 `run`、`stop`、`abandon` 的专用流程。
_Avoid_: 终端控制文件、重复消费、通用消息队列、过期授权、命令各自直接驱动 Engine、隐式授权、重复命令创建第二个 Action

**Action Admission Gate（动作准入门）**:
一个 Local Delivery Workspace 中每个 Delivery Task 对维护者 Lifecycle Action 的单槽位准入规则：同一时刻最多一个已接受但尚未完成的 Action，后来的新 Action 立即拒绝且不修改 Run、不等待、不排队。`run`、普通 `resume`、`revise` 与 `requeue` 在各自意图已按 Action identity 准确应用且 Run Executor Session 完成启动握手时释放准入；`stop` 与 `abandon` 在各自目标状态持久形成时释放准入。`approve` 及恢复原最终批准操作的 `resume` 按 [ADR 0012](../adr/0012-noncancellable-final-approval.md) 占用准入，直至完整操作成功或形成准确的无法继续边界，不能只凭批准已保存便释放；两种 Parent 发现可接纳的远端追加时，旧批准失效，原 Action 以更新启动结果释放准入，同一 Executor 自动进入可停止更新；失败后仍须对账原执行和实际结果才能恢复。其他 RunDriver 自动工作不占用该门，只读查询与其他非 Delivery Run 生命周期管理也不经过该门。Action 中断或结果未知时，完全匹配的原命令可以附着并对账；恢复原最终批准操作的 `resume` 也可在准入前对账该原操作。只有先形成原 Action 的确定终态并释放单槽，才可顺序接受 successor，恢复执行还须确认旧执行者已结束，不能仅清除忙碌标记后接受下一项。
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
下一次公开生命周期命令在接受新 Action 前，对唯一同库 Task Control 权威及其准确 Delivery Run 绑定执行的一致性核验边界。Action Application Receipt 只能核验已接受 Action 的业务应用事实，不能重建控制权威、Executor ownership 或退出证明。现有 Run 或 Receipt 要求控制权威而该权威缺失、损坏或绑定不一致时 fail closed，并给出可定位诊断；不从业务 Receipt 补造授权、不猜测旧 Executor 已退出、不启动后继。`status` 与 `history` 不执行修复或写入状态。
_Avoid_: 只读查询隐式修复、删除 busy 标记冒充恢复、裸 PID 猜测、无法确认时启动新 Executor、重放业务 Action

**Action Application Receipt（动作应用凭据）**:
Delivery Run state 为一个已接受 Lifecycle Action 保存的最小、不可歧义事实，证明该 Action identity 对应的命令特定业务意图已经幂等应用。它用于在已有同库控制权威下核验原 Action 的应用与收口，不能重建缺失控制记录或恢复授权，也不能授权 Publisher、代替 Action Receipt View、参与 Action history 展示或复制完整 Action payload；Task Control Record 只引用它，不重新解释业务状态。相关控制与业务变化按同库短事务合同提交，不依赖两份存储之间的补偿恢复。对最终批准而言，它只证明批准意图已应用，不单独证明完整交付成功，也不能替代合并与收尾的结果确认。
_Avoid_: 用户回执、Action history、第二份业务状态、重放业务意图、Publisher authority

**Action Receipt（动作回执）**:
公开生命周期命令在返回前对本次 Lifecycle Action 给出的有界、准确结果。`run`、普通 `resume`、`revise` 与 `requeue` 的成功回执说明命令特定意图已按 Action identity 幂等应用到 Delivery Run，且准确 Run Executor Session 已完成启动握手或已被确认存在；它不证明 Agent 工作、当前自动区间或整个 Delivery Run 已完成。`stop` 与 `abandon` 的成功回执只在各自准确目标状态已经持久形成后给出；`approve` 及恢复原最终批准操作的 `resume` 只在最终交付与必要收尾完成后给出成功回执，失败、结果未知或批准失效须如实说明已完成部分及当前允许的下一步。两种 Parent 自动更新的回执使用 `updating`，只证明原批准段结束并开始更新与复验；更新期间可 Stop/Resume，通过后仍需新批准。Action 接受后的中间进展属于可离开的 Action Completion Observation。启动或收口结果未知时不得伪装成功或直接创建第二个 Executor，须先依据 Host、Task Control 与 Run application receipt 对账。
_Avoid_: 以启动握手冒充 Delivery Run 或 Agent 完成、仅凭进程启动请求、模糊 success、启动未知时盲目重试

**Action Receipt View（动作回执视图）**:
公开生命周期命令默认提供的面向操作者简洁叙事，与 Delivery Progress View 使用相同语言和信息层级。它回答准确 Repository 与 Parent、这次操作实际发生了什么、Agent Run 是否已经开始或原本就在运行、当前可理解状态，以及操作者是否需要行动和可使用的下一条 `status` 命令；`stop`、`abandon`、`approve` 及恢复原最终批准操作的 `resume` 等待期间只展示少量结构化阶段变化，不输出 Worker 或宿主原始日志。成功输出保持简短，失败或结果未知时说明原因、保留了什么和如何恢复。默认视图不展示 Run ID、Action ID、SHA、digest、execution nonce、generation、Host slot、systemd unit、PID 或其他机器绑定，精确内部事实只进入 Machine Audit View 或诊断。
_Avoid_: 字段转储、内部状态码、SHA、哈希式身份、Executor Host 术语、把命令接受写成交付完成、无下一步的错误

**公开生命周期命令（Public Lifecycle Command）**:
维护者用于创建或推进 Delivery Run 的稳定交互入口。`run` 是创建或恢复 Run 的唯一普通入口，也是不携带新增恢复或批准意图的自动入口：不存在未完成 Run 且 Parent 当前可执行时创建后继 Run；已有准确 Executor 正常运行且没有未完成 Action 时只返回现有状态，不提供单独的公开 `start` 或额外 `--new-run`。`run`、`resume`、`approve`、`revise`、`requeue`、`stop` 与 `abandon` 都只提交一次对应 Lifecycle Action，确保该任务唯一的 Run Executor Session 存在，并在取得准确 Action Receipt 后返回；它们不各自在调用进程内形成不同推进路径。`run`、普通 `resume`、`revise` 与 `requeue` 在命令意图准确应用并完成启动握手后返回；`stop` 与 `abandon` 默认通过可离开的 Action Completion Observation 等待各自目标状态后返回。`approve` 及恢复原最终批准操作的 `resume` 通过同一观察方式等待完整操作的准确结果后返回，执行期间不可由其他生命周期动作取消。`stop` 是可恢复的中止意图，停止后必须显式 `resume`；`abandon` 是不可恢复的终止、对账与受限收口。Development、Acceptance、Publication 与内部等待只是 Controller 阶段，不要求也不允许维护者把它们作为独立流程手工串接；普通外部监督超时可由同一 Parent 的显式 `run` 或 `resume` 恢复；最终批准操作失败或超时后须显式 `resume`，沿用其不可取消与等待实际结果的合同。
带 `--ticket` 的 Stop/Resume 只作用于指定 Ticket；不以结束或重启整个 Executor 作为局部命令的完成条件。
_Avoid_: 公开 `start`、额外 `--new-run`、把 `stop` 当作 `abandon`、手工反复执行内部阶段、把 `deliver` 当作公开工作流、以命令顺序替代 Controller 状态机

**Operator Start（人工启动）**:
维护者通过本地显式命令选择 Parent Spec 并授权创建或恢复 Delivery Run。它是唯一启动授权，任何 GitHub 标签都不能替代。
_Avoid_: 标签触发、定时触发、自动 intake

## 监督、暂停与停止

**Run 内部监督（In-Run Supervision）**:
Run Executor Session 在可自动判定的远端异步边界（例如 Required Checks、GitHub 事件最终一致性）内自行等待、退避重试和重新读取权威事实；维护者不为普通等待另行启动 watcher 或重复输入同一命令。GitHub 读取或对账的未知非零退出默认进入有界监督，Controller 只保存经脱敏、有界的错误证据，不从 `gh` stderr 推断网络、认证、权限、代理或其他具体原因。只有结构化远端事实已证明 Publisher intent、身份、head/base、检查、状态或关闭证据矛盾时，才转换为 Human Blocker；最终人工批准仍是独立授权边界。
监督持续覆盖所有需要自动观测的工作；一个 Ticket 的 Development、Review 或其他 Agent 工作不能阻止另一个 Ticket 的 CI 观测及满足条件后的交付推进。
_Avoid_: 维护者轮询 CI、等待其他 Ticket 结束才观测、常驻的第二套控制器、自动越过 Final Human Acceptance

**执行失败（Execution Failure）**:
Delivery Run 身份已经确定后，当前自动步骤因 Agent 进程、凭据、sandbox、timeout、signal 或其他不能安全继续的执行问题而形成的持久化可恢复暂停。失败对象保留准确 Work Subject、Semantic Attempt、Thread、Managed Development Checkout 与 Failure Diagnostic Evidence 并等待显式 `resume`；单 Ticket 失败不结束仍有独立工作的 Run Executor Session，也不等同于 Action Completion Observation 离开。只有无法确定 Run 身份或无法安全持久化状态的故障才保留为命令级进程错误，并由 Host Diagnostic Log 提供最后的排查入口。
_Avoid_: 自动重试、监督超时、Human Blocker、无诊断证据的失败、丢弃未提交成果

**Failure Diagnostic Evidence（故障诊断证据）**:
Controller 在确定 Delivery Run 后为一次 Execution Failure 持久保存的有界、脱敏、可定位事实，使维护者无需依赖原终端即可判断故障发生在哪个 Work Subject、阶段与外部操作，并区分 Agent 中断、子进程失败、超时、信号、权限、持久化或宿主问题。Codex 的完整事件与对话由其自身 Session 记录负责，agent-run 只保留关联该 Invocation 与 Thread 所需的身份和失败摘要，不复制完整 Session、原始事件流或无界 stdout/stderr。`status` 展示可行动原因，Machine Audit View 保留更精确的诊断绑定。
_Avoid_: 完整 Codex Session 副本、无界 transcript、原始凭据、只有通用 error 字符串、依赖发起终端

**Host Diagnostic Log（宿主诊断日志）**:
Executor Host 交给操作系统日志设施保存的进程级诊断信息，用于在 Executor 尚未来得及写入 Failure Diagnostic Evidence 时追查启动、退出、signal、异常与宿主资源故障。它只接收 Executor 自身的有限诊断，不主动复制 Codex 原始事件流、完整子进程输出或 Delivery Run timeline；其保留时长和轮转由宿主策略决定，因此不是 Delivery Run 权威状态、恢复依据或用户历史。在 Linux 实现中，该职责由用户级 systemd journal 承担。
_Avoid_: 第二套 Run state、业务历史、完整 Agent 输出归档、无限保留、跨平台核心依赖

**Operator Stop（人工停止）**:
维护者直接结束当前 Run Executor Session 和准确 Agent 进程组、但保留 Delivery Run、Semantic Attempt、可恢复 Thread 与 Managed Development Checkout 以便后续恢复的 Run 级明确 Lifecycle Action；定向停止单票的范围见 Ticket Stop。Stop 可从任意终端提交，但不能取消执行中的最终批准操作，包括经 resume 恢复的该操作；意图获准并持久化后形成单调控制栅栏，不通知或等待 Agent 生成收尾输出，并禁止旧 Executor 开始新的 Agent、Git 或 Publisher 副作用。已经在途的单个 Publisher operation 只允许完成或进入对账，不能继续后续链式 mutation。成功 Stop 形成持久化 `operator_stopped` 边界并结束 Executor，不是执行故障或放弃 Run；后续必须显式 `resume`，Thread 已记录且仍可恢复时复用。准确 Executor 已退出但非终态 Delivery Run 记录未收口时，确认没有遗留 Worker 后，`stop` 仍持久化人工暂停；已有暂停的重复 Stop 与已完成、已放弃交付保持原状态。显式 `resume` 可对账准确旧 Executor 与 Worker 均已退出的记录；Worker 尚存时提示先 Stop，归属或退出证据不足时拒绝，不隐式终止 Worker。`status` 与 `history` 不执行这些修复。
_Avoid_: Execution Failure、Abandonment、新建 Semantic Attempt、丢弃 Agent 已落盘成果、等待 Agent 配合、中断未对账的远端写入、普通 run 隐式恢复、无声退出、已有暂停或终态时重复制造 Stop 历史

**Ticket Stop（单票停止）**:
通过 `stop --ticket` 立即中断指定 Ticket 的准确 Agent 或取消其待执行排队、保留已落盘成果与恢复依据的局部暂停；确认执行停止后释放工作名额，其他 Ticket 及其 CI 监督继续。后续需显式 Resume，持有的集成席位按异常暂停规则释放、在途远端操作先按实际结果收口，停止不清除预算等独立门禁，也不形成 Run 级停止；已确认的行为修订见 [ADR 0019](../adr/0019-ticket-work-concurrency-and-supervision.md)。
_Avoid_: 全局 Operator Stop、等待 Agent 完成本轮、结束其他 Ticket、撤回已完成远端操作、清空预算门禁

**No-progress Guard（无进展保护）**:
Run Executor Session 对一次自动步骤执行前后的确定性进展身份进行机械比较；若状态、当前 Work Subject、Generation、phase、Semantic Attempt、Invocation、Candidate、PR、准确 head、等待边界、cleanup 与下一步骤均未变化，则保存 `execution_failed/controller_no_progress` 并停止，而不以相同输入忙循环。时间戳、timeline、日志、展示字段与重试计数不构成进展；`waiting_checks`、`waiting_external` 与 `waiting_merge` 由 Run 内部监督负责，不属于无进展。
_Avoid_: 任意最大循环次数、把长时间 Agent 执行判为无进展、把外部等待判为无进展、依赖时间戳伪造进展

**监督截止时间（Supervision Deadline）**:
Run 内部监督按远端状态类别采用有限等待预算。预算内保持自动轮询与重对账；到期时保存最后的权威证据并进入监督超时暂停，而不是执行失败或假装通过。GitHub 只读状态或事件收敛的默认预算为 10 分钟，Required Checks 的默认预算为 45 分钟；其他类别须有自己的明确预算。
_Avoid_: 无限占用进程、把超时吞成 pass、把正常 pending 伪装成需要外部授权的 Human Blocker

**监督超时暂停（Supervision Timeout Pause）**:
可自动判定的远端异步状态在其监督截止时间内仍未收敛时，受影响对象保留最后的权威证据并等待维护者显式继续；Ticket 自身超时只暂停该 Ticket，其他独立工作及其监督继续，整个 Run 无自动工作时 Executor 才退出。它不要求维护者提供额外产品决策、权限或凭据；`status` 与 `history` 指向准确对象的恢复动作，Ticket 定向恢复使用 `agent-run resume <parent-issue> --ticket <number>`，Run 级监督沿用显式 `run` 或 `resume` 恢复其等待窗口，恢复监督本身不创建新的 Agent 工作或代表交付完成。
_Avoid_: execution_failed、Human Blocker、常驻 watcher、无界单次进程

**Run-wide Operator Gate（运行级人工门禁）**:
Parent-only Change Job、Run Acceptance、Run Publication 的人工门禁，以及需求或图谱合同变化、明确全局停止和确定性全局矛盾，会暂停整个 Delivery Run。Ticket Job 的 Human Blocker、预算检查点及其他局部门禁仅阻塞该 Ticket 和依赖任务；独立 Ticket 可继续开发或交付。Run-wide 门禁保留准确阻塞对象与成果，只允许对应恢复或终止动作。普通 Ticket DAG 依赖、triage 不可领取及仍在监督预算内的异步等待也不构成 Run-wide 人工门禁。
_Avoid_: 将 Ticket 局部门禁误投影为 Run-wide、在全局门禁期间切换 frontier、把普通依赖等待当作人工门禁
