---
status: accepted
---

# 每个 Delivery Task 由操作系统托管一个短生命周期 Executor

后续修订：[ADR 0011](0011-resume-interrupted-output-step.md) 为 Executor 仍存活时的 Worker 普通异常增加一次同 Thread 自动恢复，并对准确容量错误允许持续恢复，并明确失联状态的耗时展示。Executor 自身消失后不自动重放、等待显式恢复的规则继续适用。

后续有限修订（#226 / #227）：公开 `./setup.sh` 在统一确认后可一次性准备 Runner 宿主依赖，
并汇总真实 user systemd、bubblewrap 与认证检查。以下“安装器不查询或提示 systemd”仍指
底层 `./install.sh`，不限制外层 Setup 的执行就绪报告。Setup 不启用 linger、替换 init、
关闭全机安全机制或建立常驻管理器，不改变 lifecycle 提交前检查与 Executor 真实握手权威。
无前台降级、无 Executor 自动 restart、无开机 Run 重放及 Runner 管理租约保持不变。

Issue #156 暴露的不是单个 `resume` 分支遗漏，而是公开生命周期命令拥有不同进程寿命：`run` 进入统一 `RunDriver`，普通 `resume`、`requeue`、`approve`、`revise` 与 `abandon` 则各自在调用进程中直接推进部分 Engine。命令可以成功形成 `waiting_checks` 后退出，后续外部状态却无人监督；同时，当前工作区级状态锁可能在 Agent、Git、GitHub 与等待期间一直被持有，使不同 Parent 也被无关地串行化。

我们把 Delivery Run、Run Executor Session 与 CLI 命令分成三个寿命。Delivery Run 是从创建到 `completed` 或 `abandoned` 的持久交付对象；Run Executor Session 是其中一段无需新增人工授权的自动工作；CLI 只是提交 Lifecycle Action、取得 Action Receipt 或读取状态的短命客户端。一个 Local Delivery Workspace 中每个 Delivery Task 同时最多有一个未完成 Delivery Run、一个 Run Executor Session 和一个已接受但未完成的 Lifecycle Action，不同 Delivery Task 可以并行。两个独立本地仓库根目录由维护者自行协调，不建立跨 clone 唯一性。

`run` 成为创建或恢复 Delivery Run 的唯一普通入口：不存在未完成 Run 且 Parent 可执行时创建后继 Run；准确 Executor 已运行且没有未完成 Action 时，再次 `run` 只返回现有状态；移除公开 `start` 和 `--new-run`。`run`、`resume`、`approve`、`revise`、`requeue`、`stop` 与 `abandon` 都提交一次类型明确的 Lifecycle Action，由同一个 Executor 和 `RunDriver` 消费，不建设通用 Command Bus。Action Admission Gate 是每个 Delivery Task 的单槽位非阻塞门：已有未完成 Action 时，后来的新 mutation 立即失败，不等待、不排队、不改变优先级。完全匹配当前 unresolved Action 的原命令可以在准入前附着并对账原 Action；只有原 Action 被确定失败并持久收口后，同一次用户命令才可顺序提交 successor Action。`status` 与 `history` 始终只读，不经过该门。

控制面使用每个 Delivery Task 一个有界、原子写的 Task Control Record，集中保存 Local Delivery Task Index 投影、当前 Action、Executor 所有权和启动握手；Delivery Run JSON 继续只保存业务生命周期权威，并为命令特定意图保存引用 Action identity 的最小 application receipt。两者不复制业务状态。CLI 在短事务中提交 Action，然后确保准确 Executor 存在并等待本次动作的规定完成边界：`run`、`resume`、`approve`、`revise` 与 `requeue` 只有在意图幂等应用且 Executor 启动握手完成后才返回并释放准入；`stop` 与 `abandon` 观察到各自持久目标状态后返回。Run 已应用但 Task Control 尚未收口时，对账只补完原 Action，不重放业务意图。观察终端被关闭或 Ctrl-C 中断只停止观察，不撤销已经接受的 Action。

Executor Host 是平台无关 Interface，负责按准确 Local Delivery Workspace、Delivery Task、Delivery Run、执行代次与 Runner 身份启动、检查、通知和回收 Executor，但不拥有 Delivery Run 业务状态。Linux MVP 只实现 user systemd transient service Adapter，不实现 macOS 或 Windows Adapter，也不增加常驻 agent-run daemon。用户级 systemd 可用是 lifecycle mutation 的运行前置；不可用时命令在持久化新 Action 前明确失败，不静默回退为前台进程，`status` 与 `history` 仍可使用。Source Runner Installer 只强制检查 Installation Readiness，不查询或提示 systemd；`doctor` 只读报告 Installation Readiness 与 Execution Readiness，不启用 systemd 或 linger。

关闭启动终端不结束 Executor。linger 仅提供退出最后一个登录会话后的 Logout Persistence，是可选能力，不由安装器自动启用。systemd 不自动 restart Executor，主机重启也不自动重放 Delivery Run。Executor 在 Human Gate、Operator Stop、Execution Failure、Supervision Timeout Pause、`completed` 或 `abandoned` 后退出；没有自动工作时不存在 agent-run 后台进程。

Task Control Record 使用任务级短锁，Run state 与共享索引也只在读取、验证、原子提交期间持锁。任何 Agent、Git、GitHub、Publisher、sleep 或外部监督都不得在共享状态锁内运行。Executor 是当前 Delivery Task 的唯一业务写者；每次 Agent、Git 或 Publisher 副作用前必须短事务重验 ownership、generation、currentness 与 Stop/Abandon 控制栅栏，既防止旧快照覆盖，也防止已经失去授权的 Executor继续产生外部副作用。Stop/Abandon 栅栏后只允许已在途的单个 Publisher operation 完成或进入对账，不能继续后续链式 mutation。

每个 Executor Session 使用启动它的 lifecycle 命令所在终端的用户环境快照，使项目 PATH、虚拟环境、代理、自定义 SDK 与本机工具保持一致。快照通过仅当前用户可读、大小有界、绑定准确 Action/generation 且只能消费一次的临时载体传递，不进入 Run state、history、unit metadata 或日志；握手、确定失败、冲突收口、Host 证明退出或不长于握手窗口的截止时间都会删除载体，迟到 generation 不能消费。Worker 从该环境派生项目能力，但 agent-run 自有 Publisher 凭证、控制 capability、Task Control、Runner 管理和 lifecycle mutation 入口必须被机制性遮蔽；这是 ADR 0001 trusted-yolo 合同的窄控制面例外，不是通用文件系统 sandbox。

Executor 与其 Worker 必须绑定准确的任务、Run、执行代次和 Host ownership。Executor 意外退出后，不自动重放中断的 Agent Invocation：先把可证明已中断的 Invocation 形成 `execution_failed/session_interrupted`，等待显式 `resume`；纯外部监督可以从已持久的监督窗口继续；Publisher 写结果未知时先读取远端权威事实对账。无法证明旧执行者已经消失时 fail closed，不依据裸 PID 猜测或终止进程。Operator Stop 是例外的明确授权：意图和控制栅栏持久化后直接终止准确归属的 Codex Worker，不通知或等待 Agent 收尾，保留已落盘 checkout、Semantic Attempt 与可恢复 Thread，形成 `operator_stopped` 并结束 Executor；后续必须显式 `resume`。

故障诊断采用三层责任。Delivery Run 保存有界、脱敏且可恢复的 Failure Diagnostic Evidence，并将已接受 Action 及其结果保存为不参与 admission/routing 的有界审计投影；Machine Audit View 保存精确内部绑定；Linux systemd journal 只补充 Executor 启动、退出、signal 与异常等 Host Diagnostic Log。Codex 的完整对话和事件继续由 Codex Session 自身负责，agent-run 不复制完整 Session、原始事件流或无界 stdout/stderr，Executor 也不在内存中无界累计原始子进程输出。默认 Action Receipt、`status` 与 `history` 保持面向操作者的叙事，不展示 SHA、PID、systemd unit、内部 generation 或其他机器字段。

Runner Installation、Runner Rollback 与 Runner Uninstall 使用固定 Runner Management Lock 的独占非阻塞管理租约；多个 Executor 使用同一锁的共享 Runner Usage Lease。Active Runner 选择、binding claim 与租约从启动者到 Executor 的接管必须连续，pending startup 不能留下管理动作可穿插的空窗。任一共享租约存在时管理动作立即失败，不等待、不查询 systemd、不自动 `stop`、不热更新，也不为 Run 固定或引用计数旧 Snapshot；持有进程退出后由操作系统释放租约。既有 Delivery Run 后续恢复时使用届时的 Active Runner，不做旧状态迁移或兼容层。

## Considered Options

- 只把普通 `resume` 的结果重新交给当前前台 `RunDriver`：可以满足 Issue #156 原始验收条件，但不能统一其他 lifecycle mutation、终端寿命、并发控制、停止或 Runner 更新边界，因此不采用为最终架构。
- 一个工作区或用户级常驻 daemon：可以集中控制，但没有任务时仍驻留，并引入全局故障域、升级协议与额外状态；per-task transient Executor 已能提供所需所有权，因此拒绝。
- 一个进程覆盖整个 Delivery Run：会在 Human Gate 长期占用资源。Executor 只覆盖连续自动区间，人工等待期间退出。
- 裸 detached subprocess、tmux 或 terminal multiplexer：无法同时提供稳定所有权、唯一启动、状态查询、日志和未来平台 Adapter 边界，因此不作为 Linux MVP 的规范 Host。
- Lifecycle Action 队列、优先级或把后来的 `stop` 升级覆盖当前 Action：会增加过期授权与恢复组合。MVP 只允许一个未完成 Action，竞争者立即失败。
- systemd `Restart=` 或主机重启后自动恢复：无法安全判断 Agent 与外部 mutation 是否已经生效，可能造成重复工作或重复写入，因此拒绝自动重放。
- 为每个 Run 固定 Runner Snapshot：可以允许运行中更新，但需要长期 Snapshot pin、引用计数和迁移承诺。MVP 通过 Runner Management Quiescence 避免这套复杂度。

## Consequences

CLI、状态并发和进程托管必须作为一次原子语义切换落地；项目不为旧 Run state、旧 `start` 入口或旧前台行为保留兼容层。Issue #156 正文提出的“同一 CLI 进程进入 RunDriver”被本 ADR 的更强合同取代：同一 Lifecycle Action 必须由独立 Run Executor Session 继续监督，发起 CLI 在握手后可以退出。

现有 `RunDriver` 与 `ExternalSupervisor` 继续作为唯一自动推进循环；现有 Delivery Engine 不复制第二套业务状态机，但必须移除长时间持有共享 StateStore 锁的调用方式。Task Control Record、Executor Host 和 Lifecycle Action 是新增的窄 seam，不进入 Worker Prompt，也不改变 Development、Acceptance 或 Publication Agent 的局部员工合同。

本 ADR 修订 ADR 0008 的 Runner 管理后果：管理动作不再忽略运行中的 lifecycle process，而是只在 Runner Management Quiescence 中改变 Active Runner。安装器仍不驻留、不自动更新、不使用 `sudo`，Runner 也仍不绑定既有 Delivery Run。

本 ADR 修订 ADR 0009 对操作者中断的描述：关闭或 Ctrl-C 中断发起 lifecycle 命令只离开 Action Completion Observation，不再直接中断 Agent；需要停止当前自动区间时必须提交显式 `stop`。ADR 0009 关于保留 Managed Development Checkout、Invocation Resume 不重复计数和不自动重试的规则继续成立。

Linux MVP 增加 user systemd 运行依赖，但不强制 linger。以后增加 macOS 或 Windows Host Adapter 时，必须保持同一 Executor Host、Task Control Record、Action Completion 与故障恢复合同，不能把平台差异泄漏成新的公开 lifecycle 流程。

架构调研与平台证据见：

- `docs/research/2026-08-29-shared-run-lifecycle-architecture.md`
- `docs/research/2026-08-29-cross-platform-executor-host.md`
- `docs/research/2026-08-29-cli-stop-control-semantics.md`
