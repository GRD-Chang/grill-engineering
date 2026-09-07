# ADR 0003：区分 Invocation Resume、Requeue 与 Output Repair

## 状态

Accepted

后续修订：[ADR 0011](0011-resume-interrupted-output-step.md) 为 Executor 存活时的 Worker 普通异常增加一次同 Thread 自动恢复，并对准确容量错误允许持续恢复，并要求默认 Resume 保留 JSON 修复步骤与只读权限。下文“不自动重试”及恢复后的格式修复额度以该窄例外为准；其他分层继续适用。

## 决策

Development、Fresh/Run Acceptance 与 Publication 使用各自的根级 object Structured Outputs schema。
一次 Agent Invocation 包含一个初始 Output Attempt 和最多两个 Output Repair Attempt。Output Repair
只处理零退出后的本地 contract 错误，固定复用同一 Thread、只读 checkout，并在每次执行前重新核验
currentness。Controller 按 Development、Reviewer 或 Publication 角色选择短格式修复 Prompt，只提供
本地 contract 错误并要求重发本角色的合法结构化输出，不重新执行开发、审查或发布叙事工作。

进程、凭据、sandbox、timeout、signal、非零退出、缺少最终输出或 Thread mismatch 均结束为
一次 `execution_failed`，不自动重试或替换 Thread。Controller 在启动前保存 active Invocation，
收到 `thread.started` 时立即保存 reported Thread。操作者后续 `resume` 默认恢复该 Thread，只有
显式 `--new-thread` 或没有可恢复 Thread 时才以当前角色和任务模式的完整标准 Prompt 新开。恢复原
Thread 时，Controller 根据 Development、定向 Repair、Reviewer 或 Publication 角色选择短 Prompt，
只重新提供完成本轮仍然需要的动态证据；Human Blocker 还提供当前 blocker 与维护者最新回复。模型
不接收 Thread、Resume、`execution_failed`、预算或后继状态说明。Resume 是同一 Job Generation 内的
Invocation successor，不迁移或重置 Candidate、Acceptance、Human Response 或 branch/PR。只有到达
适用的预算耗尽检查点后，维护者显式 Resume 才按当前 Delivery Policy 创建新的编号预算窗口；普通
执行失败或 Human Blocker 继续原 Semantic Agent Attempt，不重置预算。

当机械 Currentness Boundary 已经漂移，Controller 不让 Resume 猜测新事实或继续旧 checkout。Ticket 与
Parent-only Change Job 停在 `requeue_required`，只能由操作者显式 `requeue`：旧 Generation 只保留有界
审计事实，新 Generation 从该命令时重新读取的权威状态获得新的 Thread，且在拥有 generation-local
branch/PR 时获得新的 branch/PR。Run Acceptance 与 Final Run Publication 不使用 generation-local
branch/PR；它们的边界漂移回到 fresh Run Acceptance。未知外部 PR mutation、Candidate/Acceptance
不一致等不可机械重建的事实仍是 Human Blocker，而不是 Requeue。

## 后果

Output Repair 不增加 Development、Reviewer 或 Publication 的领域 attempt。Candidate 与
仍 current 的 Acceptance 可在 Invocation 失败后保留。`status`/`history` 可展示有界错误与
Invocation 事实，而无需保存 stdout、Prompt、transcript 或 tool events。

这个分层使三种动作互不替代：Output Repair 只修复零退出后的格式问题；Resume 继续当前失败或
Human Blocker Invocation，并在适用的预算耗尽边界依据维护者的显式命令创建新的审计窗口；Requeue 只替换已经 stale 的 Generation。任何一层都不授予 Publisher
Mutation Authority，也不能绕过 Required Checks 或 Published-Head Gate。
