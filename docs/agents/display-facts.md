# Delivery Run 的只读展示事实

模块总览见[当前架构](../architecture.md)。本页说明当前 native 查询与通知投影的归属，
不改变业务流程、执行权限或人工批准规则。
Status、History、CLI 和通知投影从已提交事实派生，不另存展示状态；
查询不执行恢复、不发送通知、不执行 Publisher 操作。渠道选择字段、摘要和固定 Run 语言的文案，
原始结果及证据保持原文。

## 查询、渲染与通知的分工

| 事实或输出 | 当前维护归属 |
| --- | --- |
| 定位、唯一候选选择与命令分流 | [`cli/selection.py`](../../src/agent_run/cli/selection.py)、[`cli/main.py`](../../src/agent_run/cli/main.py)、[`cli/lifecycle.py`](../../src/agent_run/cli/lifecycle.py)；Task 位置记录由 [`storage/tasks.py`](../../src/agent_run/storage/tasks.py) 维护 |
| status/history/settings 的同源只读文档 | [`queries/run.py`](../../src/agent_run/queries/run.py) 的 `read` |
| 交付对象、gate、预算、候选、PR、当前工作及人工待办 | `queries.run._subject`、`queries.read_facts.read_boundaries`；结构见 [`queries/model.py`](../../src/agent_run/queries/model.py) |
| 原始调用、Thread/Profile 和物理输出关联 | `queries.run._attempt`、`_invocations`、`queries.execution.recorded_profile` |
| 原始审计与分页历史 | `queries.run._native_history`、`_control_history`、`_resume_progress` |
| Worker 活动段与累计 effort | [`queries/execution.py`](../../src/agent_run/queries/execution.py) 的 `activity`、`execution_facts` |
| 当前本机进程观察，独立于历史 | [`queries/current_observation.py`](../../src/agent_run/queries/current_observation.py)；Core 核验只读绑定，Foreground Host 提供只读物理观察 |
| JSON、plain、Rich 与 settings 文案 | [`cli/render.py`](../../src/agent_run/cli/render.py) 的 `render`、`render_settings` |
| 已提交通知事件、里程碑与 Resume 结果 | [`notifications/event_projection.py`](../../src/agent_run/notifications/event_projection.py) |
| 通知的当前 gate/完成边界与过时进展过滤 | `queries.read_facts.read_boundaries`、[`notifications/progress.py`](../../src/agent_run/notifications/progress.py) |
| 事件观察、outbox 和实际投递 | [`notifications/sender.py`](../../src/agent_run/notifications/sender.py) 的 `NativeNotifications`，以及 `agent_run.storage.notification_outbox` / `adapters.notification_transport` |

`QueryDocument` 是显示结果，不是另一套可保存或推进的 Run。
`current_observation` 是本次有界读取，不是保存的状态、执行授权或连续活性保证。
Core 从原 Run/owner/epoch、HostRequest 和准确当前 AgentCall 核验观察绑定；物理读取在
SQLite 事务外完成，再读取并比较原绑定。身份变化、读取失败、超出本次 IO 预算或证据不足
均降为 `unknown`，不结算退出、不改 lease、不推进恢复。
本机 foreground 可提供准确 namespace/start token 绑定的 `running` 或 `exited`；
远端、不可比较的观察域及尚未接入的 systemd 查询保持 `unknown`，不调用后台管理服务。
Status 的 `executor_control.live_status` 与准确当前 Invocation 的 `live_status` 从该字段派生；
History 只在独立标为“当前进程观察”的摘要中显示，不覆盖历史事件或 Invocation 的冻结事实。
不能由持久 `running` 记录推断进程仍存活。普通 Status 可单独显示同一物理输出
已记录的准确 Worker 退出及其时间；只消费原输出的启动、退出字段，不将旧 Invocation
的退出改写成当前 Host 或新 epoch 的活性证明。`scheduling` 保存实际席位、并发上限、
集成持有者及两条 FIFO；不再另造 scheduler 或 Ticket 活性镜像。
Host 或 namespace 域退出可以证明原成员已退出，却不能替代每个 Worker 的准确结束时间。
缺少独立 Worker 退出时间时，保留域退出证据，显示已记录退出、准确结束时间未知；
不得把首次发现域退出的时刻填入 Worker 的 `exited_at` 或实际执行时长。
通知观察者与 Host 的真实活动证据分别核验，不由显示布局倒推执行许可。

## 原始工作与历史

`subjects` 保存当前对象的 gate、预算、候选、PR、依赖、适用 Review、检查与写入事实。
`subjects[].review` 保留准确已记录的 Review 产物；`review_state` 使用本地与远程 Status
共用的候选、base、generation 和需求范围判定，说明该结论是否仍适用。
`stale`、`pending`、`unknown` 或 `not_recorded` 不证明当前版本已通过；旧产物仍可审计，
不能因此将旧 Findings 或通过证据显示为新候选的结论。这是持久事实投影，不是新的实时授权。

Review 的共同字段比较由 `contracts.review_evidence.same_review_candidate` 维护：需求
`revision`、`candidate_sha`、`candidate_tree`、`expected_tree`、`completion_revision`
及审核 `base_sha` 必须一致。查询从已记录的 integration snapshot（无 snapshot 时取
subject 的 integration target/base）取得比较基线；Core 从当次 World 的 target 取得基线。
该纯比较不读取数据库，也不解释通过结论或授权写入。
查询还核对原 Review 的角色、Generation、已记录及已观察到的 Parent/Graph/需求版本，
并从原产物核验 outcome；这些历史适用性不能替代现场观察。Core 的需求/Graph/Completion
当前性仍由正式推进及写前核验负责，合入另外核对最新 Required Checks、准确 PR/head/base
和人工 grant。保留两路取证与后继判断，避免将历史状态与实时授权合成带模式开关的接口。

`focus_subject` 仅引用其中一个对象，不复制当前 Worker 或预算。`pending_attempt` 使用真实当前
Attempt；其 `current_invocation` 指针在 `invocations` 中定位当前调用，不能以最后一条记录替代。
当前与历史 Attempt 共用 `result` 和 `invocations[].outputs[]` 结构。
Attempt、Invocation、Thread、Profile Revision 和物理输出通过准确身份关联；
`invocation_outputs` 的 `raw_id`、`start_evidence`、dispatch/exit 字段及活动事件供查询核验，
不以旧 `pending_semantic_attempt.returned_result` 的快照结构重建当前工作。

原始返回先由 Core 保存，再由原阶段消费。已有返回进入程序收口，不表示需要新的 Agent 调用；
Human Blocker/blocked Review 仍保留准确角色和恢复条件。展示只读取该事实，
不能清除返回、改变预算或放行 currentness。失败恢复不因一次显示读取重新调用 Reviewer 或扣预算。

每次 status/history 查询在一个只读事务内读取当前概览；history 在该事务中附加 `history` 页。
`history.events` 按原事件的 `position` 排序，是 JSON 与人类输出共同使用的唯一事件流。
`after`/`limit` 与 `history.after/next_after/has_more` 明确表示分页范围。先选事件页，再读取其
Attempt/Invocation、Effect、控制 Action 与原材料闭包；当前概览不能替代这些历史身份。
`history.attempts[].invocations[].outputs[]` 保留每次物理输出与原始返回，
`history.effects/effect_failures` 保留写入操作与实际失败，`history.rounds` 按本页 Attempt 闭包
统计领域角色轮次。`history.actions[].resume` 记录原 Action 的应用、successor 与准确目标进展；
`latest_resume` 是当前最近 Resume Action，包含相同结构，不由本页末条事件猜测。
不能以当前 Run 状态补造已裁剪的历史、结束时间、预算或验收结论。
原始 Review Context、文案 Attempt 与批准事实不随当前证据失效而改写。

## 计时与实际执行证据

`worker_activity` 保存准确 `(invocation, ordinal)` 的活动事件与证据引用。
`activity` 仅累计可配对的活动区间；缺少边界、重复启动或尚未退出时标记计时不完整，
保留可证明的 `recorded_execution_seconds`，不从观察中断或后继启动时刻补造终点。

物理输出的 `process_span_seconds` 是已记录进程启动至退出的跨度，不能代替 Agent 活动耗时。
`elapsed_seconds`、`execution_timing_complete` 与各输出的活动事实分别展示；
真实 Thread/Profile 绑定及已记录轮次不会因计时不完整而抹去。
Worker 进程登记不代表已放行。bootstrap gate 放行后的独立事件才开启执行段，
此前的持久化、控制与 deadline 检查不计入执行；gate 前拒绝不得留下已开始执行的边界。

只有明确的未启动证据才允许计零；requested Thread、调用次数为零或空活动列表不能单独证明零耗时。
`execution_facts` 按原历史位置、准确 Attempt 和 Profile 汇总已记录工作，
plain、Rich 与通知分别消费这些事实，缺少完整证据时不把未知值显示为已执行秒数。

## 外部写入、CI 与批准的展示

正式写入中断的对账沿用 [ADR 0022](../adr/0022-publication-write-reconciliation.md)。
`subjects[].pending_writes` 来自未收口的 `effects`；`scheduling.integration_holder` 保存实际
集成持有者，不再另存单项 pending-write 或 holds-integration 投影。
发布暂停不表示写入失败或未发生，查询不能清除意图、释放席位或替代远端核验。
历史操作和失败证据由 `effects`、`effect_failures` 与原事件关联，不复制旧 Job 写入快照。

需求、集成和 CI 的预算从准确 subject/purpose/window 关系读取。
两种 Parent 始终要求 Review，修复工作与最终 PR 显示同一 Run 的用量；新失败版本不重置。
Status/History 只读展示当前额度和续期历史，不补造旧内外层计数或执行续期。

审核和发布适用性由 Core 核验，展示不自行用 tree 或 passed 判断交付权限。
文案单独失效保留适用 Review；CI 与批准继续绑定准确 PR/head 和原授权对象。
普通 resume/message 不表示批准，待确认的正式写入仍显示原操作并先核验结果。

完整 Parent 的修复展示准确 subject、Generation、Candidate、工作区，以及 `run_repairs` 中的修复分支、
PR 目标、检查与合入记录。`subjects[].repair_pr` 引用当前开放记录，`repair_history` 保留已合入记录；
它们从同一权威关系读取，不建立展示状态或另收费的工作。修复检查等待与最终 PR 检查等待分属各自 PR，
明确未配置检查显示 `none`，不称 CI 通过。修复合入不表示最终 PR 检查、人工批准或默认分支交付完成。
未知写入保留原意图；status/history 不能把暂停解释为未发生，也不能代替对账或恢复。

## 通知与验证入口

Worker 的进程绑定先于最终来源核验和 GO 放行，`worker_started` 只证明 bootstrap 已被准确持有。
首次启动及人工 Resume 的启动进展采用 `worker_activity` 中首次实际 backend active 观察；
`worker_active` 历史项只引用准确 Invocation/output 的该原记录，供既有通知和只读查询关联，
不建立另一份开始状态。Stop、旧代次或已过期调用的迟到活动仍保留原执行证据，但不生成当前启动里程碑。

通知的 concise/detailed 选择、occurrence/dedupe、outbox、Ledger 和 transport
由通知模块负责；`NativeNotifications` 消费已提交事件与 `read_boundaries`，
不调用 Kernel 推进业务，也不从渲染后的 Status 文案推断新业务事实。
查询入口不接入发送过程，投递 receipt 不能作为工作结果、审核或执行授权。

现有代码核查入口包括 `tests/test_kernel_query_cli.py`、
`tests/test_kernel_notification_events.py`、`tests/test_kernel_notifications.py`。
共享测试支撑从 `tests/support` 导入，不从测试用例模块导入构造器。
测试 world 与当前配置归属见架构概览；这里的模块映射不表示上述测试已经重新执行，
也不替代正式验收结果。
