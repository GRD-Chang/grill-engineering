---
status: accepted
---

# 同 Parent Ticket 开发与审核有界并行

## 决定

一个 Delivery Run 的有效策略固定 `ticket_development_workers`，默认 3，正整数，1 为串行。该额度只计算独立 Ticket 的需求开发和原有 Review lane；队列等待、CI 观测、集成及 Agent 内部子进程不占 lane。依赖只由已完成合入和收尾记录解除。Ticket 的开发及 Review 在各自固定工作区并行；集成、发布、CI 与合并继续沿用共享交付 Engine 和单一 Run Branch 集成席位。

`ticket_jobs` 与 `ticket_agent_invocations` 按 Ticket 保存规范事实。开发 lane 使用 Ticket 局部投影调用共享 Engine，每次保存均在 StateStore 的 Task Control 围栏和文件锁下重新读取最新 Run，只归并当前 Ticket 的 Job、Invocation 与历史。对同一 Ticket 已变化的 Job 拒绝旧写入；全局停止或合同失效时拒绝 lane 提交。一条 lane 完成后，编排可在其他 lane 继续运行时推进空闲的集成席位。Controller 的整状态刷新只在提交时与 lane 归并短暂互斥；随后的集成交付可等待 Agent，同时其他 lane 继续保存。Ticket 交付每次持久化前在同一 StateStore 事务中从最新状态归并其他 Ticket 的 Job、Invocation、历史、暂停请求和队列位置，包括已排队且被立即暂停的 Ticket；尚未落盘的开发 lane 也受保护。当前交付 Ticket 的变更仍由该交付负责，全局停止后拒绝旧写入。`active_ticket_job` 与 `active_agent_invocation` 只是当前调度对象的投影。新状态协议为 `multi_ticket_protocol=2`；旧协议在写入前明确拒绝，不迁移。

Controller 读取 GitHub 图时不持有状态锁。刷新开始时记录 Run 快照，提交时在 StateStore 事务内确认快照未被其他写入改变；发生冲突则基于最新状态重新刷新，显式操作要求调用方重试，不能把旧快照覆盖到新的局部暂停或控制状态。刷新返回前再次核对持久的 Ticket 调度和 Run 控制状态；全局停止保持停止，局部人工门禁允许重新选择仍可执行的 Ticket。

一个 Ticket 的执行异常保留其工作区、当前 phase 和失败证据，局部暂停；其他独立 Ticket 继续。`stop --ticket` 持久记录局部暂停请求，Ticket 在 Agent 完成后的安全边界停下，已排队且未持席的 Ticket 可立即暂停；维护者用定向 `resume --ticket` 恢复该 Job。若 Agent 结果先触发预算耗尽或其他独立人工门禁，该门禁先落盘并消费暂停请求，不能用局部暂停覆盖预算授权。全局 Stop 和进程失联将所有仍运行的 Ticket Invocation 标记为中断，保留语义尝试与线程依据。一个 Parent Executor 可以绑定多个 Worker PID；控制操作核验全部进程身份并终止全部存活 Worker，旧执行者失去 Task Control 授权后不能继续提交。

## 职责与代价

`run_orchestration` 选择阶段，`ticket_queue` 管理准入与集成席位，`ticket_development_state` 负责局部投影和原子归并；Change Delivery Engine 仍负责 Dev–Review 和交付，Publisher 仍负责 Git/GitHub 写入。并发没有增加第二套交付流程或状态文件。Reviewer 临时 checkout 保持 Ticket 标签隔离，失败/暂停保留工作现场，完成后的清理由原 Delivery Cleanup 负责。

事件屏障与独立临时 Git 用例覆盖重叠、上限、依赖、乱序归并、开发中进入 CI 并持席、竞争写入和局部失败；多 Worker 控制用例覆盖精确绑定及停止。新增测试只复用现有 fixture 和 StateStore，未增加付费 Agent 或线上 GitHub 设施。完整套件的同条件耗时及剩余真实进程覆盖以本次开发验证记录为准，不通过放宽超时掩盖成本。
