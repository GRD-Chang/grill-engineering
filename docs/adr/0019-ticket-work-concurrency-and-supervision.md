---
status: accepted
---

# 统一 Ticket 工作并发额度，保留单席集成交付

同一 Delivery Run 保留固定基线上的并行 Ticket 工作与单一 Run Branch 集成席位：普通远端追加在交付时接纳，正常集成、发布、CI 等待及合入期间持席，CI 代码修复与异常暂停释放。多个集成候选同时推进会增加版本失效与 CI 重跑；当前选择让开发利用等待时间，同时保持集成交付顺序简单。

需求开发、Review、CI 修复、冲突处理等实际 Agent 工作共用 Ticket Work Concurrency，纯 CI 等待、排队与程序监督不占名额。Runner 必须在其他 Ticket 执行长时间 Agent 工作时持续观测 CI，并推进已满足交付条件的 Ticket；并发上限为 1 也不能停止外部监督。统一额度防止修复阶段额外扩大资源使用，但不表示控制 Agent 内部所有子进程的总数。

本决定修订 ADR 0018 中仅计算需求开发和原有 Review 的范围，保留 ADR 0015/0016 的固定开发基线、集成与审核规则，以及各类预算的独立归属。Development–Review 与集成交付共用实现不取消 Ticket fallback、完整 Parent 验收或准确当前版本的发布依据。

其他 Ticket 仍在执行时，允许人工 Resume 一个局部暂停的 Ticket。请求被接受后，有名额就继续，无名额则持久排队，名额释放且执行条件仍满足时自动开始，无需再次 Resume。修复、恢复和新开发按照工作就绪后的等待顺序共享名额，不因工作类型自动插队，不抢占当前工作；集成席位仍单独排队。并发名额与次数预算是不同约束，既有预算只按各自显式恢复规则续期。

Ticket 执行失败、人工阻塞、预算耗尽和自身 CI 监督超时只暂停该 Ticket；依赖它的工作等待，独立工作继续。只有全局停止、Parent/依赖图合同变化或已证实的全 Run 共享授权失效等全局门禁才阻止整个 Run。所有剩余工作均需人工处理时退出自动执行并保留现场。这一边界避免将局部失败放大为整个 Run 停摆，也不让公平排队绕过人工门禁。

运行中定向 Resume 修订 ADR 0010 在该局部操作上的准入限制；它不要求建立通用 Lifecycle Action 队列，也不增加同一 Delivery Task 的第二个 Executor。

单 Ticket Stop 立即中断准确归属该 Ticket 的 Agent，不等待其完成当前调用或输出收尾；保留已落盘成果与恢复依据，确认执行停止后释放工作名额，其他 Ticket 及其监督继续。排队中的 Ticket 则取消排队并保持暂停；若持有集成席位，则沿用异常暂停的释放规则，在途远端写入结果未知时先对账。停止本身不撤销已完成的外部操作，不清除预算等独立人工门禁，也不形成 Run 级停止。这取代 ADR 0018 中等待 Agent 返回安全边界才暂停的决定，以免停止一个长调用仍长期占用名额。

Linux 继续由 systemd 托管 Executor；关闭发起终端不影响执行，Executor 崩溃或主机重启后保留现场并等待人工 Resume。本轮不增加自动重启、自动重放或常驻的第二套管理器：持续多线监督由业务调度保证，宿主故障恢复保持明确的人工边界。

## 交付状态

Development–Review 已接入共同阶段循环与审核记账：Ticket、Parent-only 的 Dev-first，以及集成后完整 Parent 的 Review-first；Run Acceptance/Run Repair 的 Review Budget 采用唯一 owner。集成交付已提供共同的单步结果：ChangeDeliveryEngine 与 Final Run Publication 的完整入口都复用各自的 advance 核心，返回可继续、等待、需要 Agent、暂停或完成；既有 Job/Run 状态继续作为唯一持久依据。Ticket、Parent-only 与 Run Repair 共用审核前/发布前集成门禁，固定需求基线和各类预算保持独立。Publication 文案在推送和 CI 之前按需形成；相同已发布版本继续观测 CI 和合入无需再次调用 Agent。

持续多线监督已接入同一 Executor 的有界工作线程：开发与审核占用既有名额时，单独的程序监督持续推进当前集成席位，不需要其他 Agent 返回。只有一个候选或并发为 1 时使用同一路径。开发票的外部等待不占工作名额，按退避重新就绪或到期局部暂停；本地到期判断独立于集成侧外部读取。Required Checks 的重试时间和绝对截止时间按 Ticket 保存；重试判断不 sleep，调度等待与外部操作不占共享状态锁。同一发布 head 继续使用现有发布结果通过共同交付 Engine 合入与收尾。

开发、集成与 CI 共用 `TicketState` 的单票事务：校验 Job、Invocation、需求身份及集成席位，按票归并历史、恢复关联和准确分支清理项。删除 StateStore 的“从整 Run 快照补回其他 lane”回写路径；Controller 的远端刷新仍用已有乐观并发检查。状态协议升为 `multi_ticket_protocol=4`，不迁移旧 Run。局部 CI 监督超时释放席位，显式 Resume 保留同一候选、PR 与业务预算。

所有 Ticket Agent 角色现共用 `ChangeDeliveryEngine.require_agent_work` 准入边界。`ticket_work_queue` 保存唯一等待顺序、Job Generation、角色与当前执行租约；同批需求工作沿用 frontier 顺序，重复就绪保留原位置。运行中的项与等待项共用这一规范记录，展示不另外记额度。`ticket_development_workers` 沿用既有正整数配置与默认 3，其覆盖范围已扩展为所有实际 Ticket Agent 工作；不承诺约束 Agent 内部所有进程。

`ticket_scheduling` 在工作结束后补位，不再等待开发批次全部结束。调度按租约与已发出的待准入工作补位，为 Agent 返回后的程序收尾保留有界线程余量，最多 2N 个需求执行片段加一个监督线程（N 为固定工作额度），这不是另一个 Agent 额度。Agent 返回或抛错立即释放准确租约，调用准备异常另由 stage 的 finally 收口；过期租约不能释放后来工作。等待 CI、集成队列及外部退避不占名额。集成侧若需要 publication 或 conflict Agent，进入同一个 FIFO，不阻塞等待名额；程序监督仍由独立有界线程推进。完成 Ticket 后通过 Controller 刷新依赖 frontier，新解除依赖的工作可以在其他 Agent 尚未结束时启动。

集成席位仍由 `ticket_queue` 管理：CI 正常等待持席，代码修复和异常暂停释放，修复候选重新排队；不得持工作名额等待集成席位。Ticket 结果保存只更新本票，并回读最新工作队列；宿主退出经既有人工恢复边界确认后清理旧租约，Run 级故障显式恢复也覆盖准入落盘但 Invocation 尚未建立的窗口，不自动重放。运行中定向恢复和单票立即停止仍是后继工作。
