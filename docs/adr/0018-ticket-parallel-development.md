---
status: accepted
---

# 同 Parent Ticket 开发与审核有界并行

后续需求修订：[ADR 0019](0019-ticket-work-concurrency-and-supervision.md) 将并发额度扩大到 Ticket 的实际 Agent 工作，要求 CI 监督独立于其他 Ticket 的长调用，允许运行中定向 Resume 排队，并将单票 Stop 改为立即中断准确归属的 Agent。持续监督与按票状态事务已实施，其余修订仍为后续工作；下文仅计算需求开发和原有 Review lane 的范围，以及等待 Agent 返回安全边界才暂停的规则由其取代，其余未修订决定继续适用。

## 决定

一个 Delivery Run 的有效策略固定 `ticket_development_workers`，默认 3，正整数，1 为单个开发 lane，仍独立进行 CI 监督。该额度只计算独立 Ticket 的需求开发和原有 Review lane；队列等待、CI 观测、集成及 Agent 内部子进程不占 lane。依赖只由已完成合入和收尾记录解除。Ticket 的开发及 Review 在各自固定工作区并行；集成、发布、CI 与合并继续沿用共享交付 Engine 和单一 Run Branch 集成席位。

`ticket_jobs` 与 `ticket_agent_invocations` 按 Ticket 保存规范事实。开发、集成、CI 和收尾共用 `TicketState` 局部投影，每次保存均在 StateStore 的 Task Control 围栏和文件锁下重读最新 Run，只提交本票拥有的字段。Job、Invocation、需求变化或全局停止拒绝迟到提交；其他票的暂停、队列和结果保留。`active_ticket_job` 与 `active_agent_invocation` 是当前调度投影。Controller 刷新与外部读取不持共享状态锁。当前协议为 `multi_ticket_protocol=4`，旧协议在写入前明确拒绝，不迁移；监督与归属细节见 ADR 0019。

Controller 读取 GitHub 图时不持有状态锁。刷新开始时记录 Run 快照，提交时在 StateStore 事务内确认快照未被其他写入改变；发生冲突则基于最新状态重新刷新，显式操作要求调用方重试，不能把旧快照覆盖到新的局部暂停或控制状态。刷新返回前再次核对持久的 Ticket 调度和 Run 控制状态；全局停止保持停止，局部人工门禁允许重新选择仍可执行的 Ticket。

一个 Ticket 的执行异常保留其工作区、当前 phase 和失败证据，局部暂停；其他独立 Ticket 继续。`stop --ticket` 持久记录局部暂停请求，Ticket 在 Agent 完成后的安全边界停下，已排队且未持席的 Ticket 可立即暂停；维护者用定向 `resume --ticket` 恢复该 Job。若 Agent 结果先触发预算耗尽或其他独立人工门禁，该门禁先落盘并消费暂停请求，不能用局部暂停覆盖预算授权。全局 Stop 和进程失联将所有仍运行的 Ticket Invocation 标记为中断，保留语义尝试与线程依据。一个 Parent Executor 可以绑定多个 Worker PID；控制操作核验全部进程身份并终止全部存活 Worker，旧执行者失去 Task Control 授权后不能继续提交。

## 职责与代价

`run_orchestration` 选择阶段，`ticket_queue` 管理准入与集成席位，`ticket_state` 负责局部投影和原子归并；Change Delivery Engine 仍负责 Dev–Review 和交付，Publisher 仍负责 Git/GitHub 写入。并发没有增加第二套交付流程或状态文件。Reviewer 临时 checkout 保持 Ticket 标签隔离，失败/暂停保留工作现场，完成后的清理由原 Delivery Cleanup 负责。

事件屏障与独立临时 Git 用例覆盖重叠、上限、依赖、乱序归并、开发中进入 CI 并持席、竞争写入和局部失败；多 Worker 控制用例覆盖精确绑定及停止。新增测试只复用现有 fixture 和 StateStore，未增加付费 Agent 或线上 GitHub 设施。完整套件的同条件耗时及剩余真实进程覆盖以本次开发验证记录为准，不通过放宽超时掩盖成本。
