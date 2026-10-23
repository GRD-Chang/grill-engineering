# Delivery Run 的只读展示事实

模块总览见[当前架构](../architecture.md)。本页说明当前 native 查询与通知投影的归属，
不改变业务流程、执行权限或人工批准规则。
Status、History、CLI 和通知投影从已提交事实派生，不另存展示状态；
查询不执行恢复、不发送通知、不执行 Publisher 操作。渠道选择字段、摘要和固定 Run 语言的文案，
原始结果及证据保持原文。

## 查询、渲染与通知的分工

| 事实或输出 | 当前维护归属 |
| --- | --- |
| 定位、唯一候选选择与命令分流 | [`query_cli.py`](../../src/agent_run/cli/main.py)、[`task_location.py`](../../src/agent_run/storage/tasks.py) |
| status/history/settings 的同源只读文档 | [`query.py`](../../src/agent_run/queries/run.py) 的 `read` |
| 交付对象、gate、预算、候选、PR、当前工作及人工待办 | `query._subject`、`_status_fields`、`_operator_actions`、`_multi_ticket` |
| 原始调用、Thread/Profile 和物理输出关联 | `query._invocations`、`execution_facts.recorded_profile` |
| 原始审计与分页历史 | `query._native_history`、`_control_history`、`_resume_history` |
| Worker 活动段与累计 effort | [`execution_facts.py`](../../src/agent_run/queries/execution.py) 的 `activity`、`execution_facts` |
| JSON、plain、Rich 与 settings 文案 | [`query_render.py`](../../src/agent_run/cli/render.py) 的 `render`、`render_settings` |
| 已提交通知事件、里程碑与 Resume 结果 | [`notification_events.py`](../../src/agent_run/notifications/event_projection.py) |
| 通知的当前 gate/完成边界与过时进展过滤 | `query.notification_boundaries`、[`notification_progress.py`](../../src/agent_run/notifications/progress.py) |
| 事件观察、outbox 和实际投递 | [`notifications.py`](../../src/agent_run/notifications/sender.py) 的 `NativeNotifications`，以及 `agent_run.storage.notification_outbox` / `adapters.notification_transport` |

`QueryDocument` 是显示结果，不是另一套可保存或推进的 Run。
当前 `scheduler.activity`、调用 `live_status` 和 Ticket `execution_activity` 在缺少实时证明时为
`unknown`；查询不探测或恢复 Host，不能由持久 `running` 记录推断进程仍存活。
通知观察者与 Host 的真实活动证据分别核验，不由显示布局倒推执行许可。

## 原始工作与历史

`subjects` 指向当前 Attempt、适用 Review、检查与写入事实。
Attempt、Invocation、Thread、Profile Revision 和物理输出通过准确身份关联；
`invocation_outputs` 的 `raw_id`、`start_evidence`、dispatch/exit 字段及活动事件供查询核验，
不以旧 `pending_semantic_attempt.returned_result` 的快照结构重建当前工作。

原始返回先由 Core 保存，再由原阶段消费。已有返回进入程序收口，不表示需要新的 Agent 调用；
Human Blocker/blocked Review 仍保留准确角色和恢复条件。展示只读取该事实，
不能清除返回、改变预算或放行 currentness。失败恢复不因一次显示读取重新调用 Reviewer 或扣预算。

history 按 `history.position` 顺序读取原事件，`after`/`limit` 与
`history_page.next_after/has_more` 明确表示分页范围。事件携带原材料、Generation、
Attempt/Invocation、控制 Action 与 Resume 审计的准确关联。
不能以当前 Run 状态补造已裁剪的历史、结束时间、预算或验收结论。
原始 Review Context、文案 Attempt 与批准事实不随当前证据失效而改写。

## 计时与实际执行证据

`worker_activity` 保存准确 `(invocation, ordinal)` 的活动事件与证据引用。
`activity` 仅累计可配对的活动区间；缺少边界、重复启动或尚未退出时标记计时不完整，
保留可证明的 `recorded_execution_seconds`，不从观察中断或后继启动时刻补造终点。

调用 `process_span_seconds` 是已记录进程启动至退出的跨度，不能代替 Agent 活动耗时。
`elapsed_seconds`、`execution_timing_complete` 与各输出的活动事实分别展示；
真实 Thread/Profile 绑定及已记录轮次不会因计时不完整而抹去。
Worker 进程登记不代表已放行。bootstrap gate 放行后的独立事件才开启执行段，
此前的持久化、控制与 deadline 检查不计入执行；gate 前拒绝不得留下已开始执行的边界。

只有明确的未启动证据才允许计零；requested Thread、调用次数为零或空活动列表不能单独证明零耗时。
`execution_facts` 按原历史位置、准确 Attempt 和 Profile 汇总已记录工作，
plain、Rich 与通知分别消费这些事实，缺少完整证据时不把未知值显示为已执行秒数。

## 外部写入、CI 与批准的展示

正式写入中断的对账沿用 [ADR 0022](../adr/0022-publication-write-reconciliation.md)。
`subjects[].pending_writes` 来自未收口的 `effects`；
`multi_ticket.tickets[].pending_write` 是其单项投影，`holds_integration` 来自当前集成持有者。
发布暂停不表示写入失败或未发生，查询不能清除意图、释放席位或替代远端核验。
历史操作和失败证据由 `effects`、`effect_failures` 与原事件关联，不复制旧 Job 写入快照。

需求、集成和 CI 的预算从准确 subject/purpose/window 关系读取。
两种 Parent 始终要求 Review，修复工作与最终 PR 显示同一 Run 的用量；新失败版本不重置。
Status/History 只读展示当前额度和续期历史，不补造旧内外层计数或执行续期。

审核和发布适用性由 Core 核验，展示不自行用 tree 或 passed 判断交付权限。
文案单独失效保留适用 Review；CI 与批准继续绑定准确 PR/head 和原授权对象。
普通 resume/message 不表示批准，待确认的正式写入仍显示原操作并先核验结果。

完整 Parent 的修复展示准确 subject、Generation、Candidate、工作区与 Run Branch 待完成操作，
不显示内部 Repair PR 发布、CI 等待或合并待办。历史既有 PR 记录保留原含义，
不补造当前修复 PR。修复完成不表示最终 PR CI、人工批准或默认分支交付完成。
未知 Run Branch 推送保留原写入意图；status/history 不能把暂停解释为未写入，也不能代替对账或恢复。

## 通知与验证入口

通知的 concise/detailed 选择、occurrence/dedupe、outbox、Ledger 和 transport
由通知模块负责；`NativeNotifications` 消费已提交事件与 `notification_boundaries`，
不调用 Kernel 推进业务，也不从渲染后的 Status 文案推断新业务事实。
查询入口不接入发送过程，投递 receipt 不能作为工作结果、审核或执行授权。

现有代码核查入口包括 `tests/test_kernel_query_cli.py`、
`tests/test_kernel_notification_events.py`、`tests/test_kernel_notifications.py`。
共享测试支撑从 `tests/support` 导入，不从测试用例模块导入构造器。
测试 world 与当前配置归属见架构概览；这里的模块映射不表示上述测试已经重新执行，
也不替代正式验收结果。
