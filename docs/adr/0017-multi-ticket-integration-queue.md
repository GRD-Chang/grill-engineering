---
status: accepted
---

# 多 Ticket 开发推进与单一集成席位

## 决定

一个 Delivery Run 为每张 Ticket 持久保存独立 Job、开发工作区、Invocation、预算与恢复事实。只有依赖 Ticket 完成合入及收尾记录后才解除本地依赖。Run Policy Snapshot 固定正整数 `ticket_development_workers`，默认 3；它限制可推进的 Ticket 开发及原有审核工作，不代表 Agent 进程并发。本阶段 Agent Invocation 保持串行，队列等待和 CI 观测不占开发 Worker。

通过开发条件的 Ticket 进入持久集成队列，按 `ready_at` 再按 Ticket number 排序。一个 Run Branch 只有一个集成席位；正常集成、发布、等待 CI、合入及确认保留席位。代码修复、无法解决的冲突或异常暂停释放席位，修复完成以新时间重新入队。其他 Ticket 可继续开发，但集成使用最新已合入的 Run Branch，不使用前一个 Ticket 尚未合入的候选。队列、席位和各 Ticket 局部门禁由 Controller 单一写入，状态查询只读投影。

新规范状态使用 `multi_ticket_protocol=1` 与 Delivery Policy Protocol 6。旧 Run 不推断队列或额度；在外部写入前按 MVP 状态合同拒绝不兼容记录，不做隐式迁移。`merged` 只表示合并已发生，收尾记录形成 `completed` 后才解除依赖。需求或图谱变化和显式全局停止仍由 Run 级门禁处理。

多个 Ticket 同时处于局部人工门禁时，维护者使用 `agent-run resume <parent> --repo <owner/repo> --ticket <number>` 选择一个恢复；恢复意图绑定该 Ticket 的当前身份，不能顺带解除其他 Ticket 的门禁。

Ticket 的 Publication Operation Retry 耗尽属于局部异常暂停，释放集成席位且不自动重试。针对该 Ticket 的显式 Resume 保存已耗尽窗口记录，开启新的有界操作重试窗口，并以新的 `ready_at` 排在已有就绪 Ticket 之后；不刷新开发、CI 或重新同步额度。重新取得席位后复核最新 Run Branch 和远端发布事实，再继续原语义成果适用的交付步骤。

## 理由与边界

单一集成席位串行化 Run Branch 写入，允许独立 Ticket 在等待 CI 期间继续开发。队列和持有者持久化，使进程失联后可先对账再继续，避免凭内存状态重复发布。此决定不引入并行 Agent 执行、第二份状态权威或自动迁移器。

## 实现职责与审查

`graph` 依据依赖完成记录生成可执行集合，`ticket_queue` 负责 Worker 准入、就绪排序与席位选择；Controller 在读回 GitHub 事实后持久化选择。Ticket 需求开发与 CI 修复继续走同一个 Change Delivery Engine，发布和合入仍由 Publisher 授权，StateStore 与 Task Control 维持单写入围栏。Parent-only 与最终 Run Acceptance 沿用原有共享交付路径，不读取 Ticket 队列。

独立 Standards/Spec 审查发现 Worker 上限未参与准入、定向恢复核验了错误 Ticket 标签、状态为不可恢复的阻塞给出 `resume` 命令。已分别在队列选择、Controller 标签核验和状态投影中修正，并以调度、双 Ticket 恢复及状态测试复验。调整 Worker 上限归属策略与队列模块；调整 CI 第二轮审核仍归属原 CI 恢复路径，远端追加提交仍归属原集成路径，不在队列模块复制这些规则。
