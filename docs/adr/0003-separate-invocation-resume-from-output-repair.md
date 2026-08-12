# ADR 0003：区分 Invocation Resume 与 Output Repair

## 状态

Accepted

## 决策

Publication 与 Final Publication 使用根级 object Structured Outputs schema。一次 Agent
Invocation 包含一个初始 Output Attempt 和最多两个 Output Repair Attempt。Repair 只处理
零退出后的本地 contract 错误，固定复用同一 Thread、只读 checkout，并在每次执行前重新核验
currentness。

进程、凭据、sandbox、timeout、signal、非零退出、缺少最终输出或 Thread mismatch 均结束为
一次 `execution_failed`，不自动重试或替换 Thread。Controller 在启动前保存 active Invocation，
收到 `thread.started` 时立即保存 reported Thread。操作者后续 `resume` 默认恢复该 Thread，只有
显式 `--new-thread` 或没有可恢复 Thread 时才以标准阶段 Prompt 新开。

## 后果

Output Repair 不增加 Development、Fresh Acceptance 或领域 Publication attempt。Candidate 与
仍 current 的 Acceptance 可在 Invocation 失败后保留。`status`/`history` 可展示有界错误与
Invocation 事实，而无需保存 stdout、Prompt、transcript 或 tool events。
