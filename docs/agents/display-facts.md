# Delivery Run 的只读展示事实

Status、History、CLI 和 Notification 从既有 Run state 派生事实，不另存展示状态，不执行恢复、发送或 Publisher 操作。渠道自行选择字段、摘要和固定 Run 语言下的文案；原始结果及证据保持原文。

Status/History 的 `scheduler` 从准确 Executor 的活性与既有 `ticket_resume_closed` 派生，区分进程仍活着和调度正在收尾。收尾时保留已知 Agent 活性，同时明确不接受新恢复；不能继续承诺后台检查或新任务派发。此投影不新增持久状态，查询不执行恢复或通知。

| 规则 | 维护归属 | 消费者 |
| --- | --- | --- |
| 暂停前继续阶段、实际工作角色与身份 | `work_facts.py` | resume、Status、History、人工待办、State 查询 |
| 角色族、当前 Work Subject、Candidate/PR 诊断身份、Ticket 进度 | `delivery_facts.py` | Status、CLI、History、通知、State 查询 |
| 全部 Operator Gate 对象枚举；对象、Generation、原始证据、局部/全局范围及结构化恢复动作 | `operator_gate.py` / `operator_action_facts.py` | Status、History、操作指引、通知 |
| 按真实 Attempt ID 去重、pending 选择、机器 JSON 字段筛选 | `attempt_projection.py` | CLI、通知、Status 计时 |
| 当前候选验收、发布边界、未解决 Findings | `acceptance_facts.py` | Status、通知；History 共用 Findings 提取 |
| Attempt/Invocation 关联、历史转折、生命周期聚合 | `history_facts.py` | History、通知、Status 计时、Ticket effort |
| 预算快照、准确 Reviewer/候选及支持证据匹配 | `attempt_evidence.py` | History 事实及机器历史投影 |
| Invocation 实际启动、活性、可信执行段累计、Run 历时及完整性汇总 | `execution_timing.py` | Status、History、通知、Ticket effort |

`delivery_history.py` 保留人类布局、详情、显示标签及原有机器历史视图的显式字段筛选；语义记录从 `history_facts.history_records` 取得，再添加渠道文案。事实层不从 CLI、Status 或 History renderer 获取数据。不能以当前 Run 状态补造已裁剪的历史、结束时间、预算或验收结论。计时事实区分完整累计与已记录片段；缺失记录不能抹去可靠的配置与轮次证据，不能用观察中断或下一次启动时刻补造执行终点。

计时需核对实际 Thread 与 Worker 边界证据。Invocation 的 `started_at` / `ended_at` 只描述调用生命周期，包含准备和 Controller 检查，不能用于推算执行耗时。`execution_segments` 保存 Worker 启动与退出形成的独立执行段；Output Repair、自动恢复与同轮 Resume 累加真实段，排除凭据准备、前后置 currentness 检查、结果校验、段间检查和等待。Worker 段未结束时只在活性可靠的当前段提供实时值；未观测到的结束不能以 Invocation 完成或发现中断时间补齐。

Worker 进程登记不代表已放行。bootstrap gate 放行后的独立事件才开启执行段，此前的持久化、控制与 deadline 检查不计入执行；gate 前拒绝不得留下已开始执行的边界。

缺少段边界的旧记录或部分记录标记计时不完整；保留可证明的闭合段累计、配置与轮次。调用方明确证明本次尚未执行而失败时，`execution_not_started=true` 才允许计零；requested Thread、调用次数为零或空段列表均不能单独证明零耗时。正式验收通过事件无法关联对应历史时，也必须显示计时不完整。

人工待办中的触发 Agent 摘要按相同 Semantic Attempt 消费 History 的累计与完整性事实，不只统计最新 Invocation。plain 与 Rich 共用该摘要；缺段时保留模型、推理强度及已记录执行时长，不输出未知值秒数。

通知的事件外壳、稳定 ID 和工作对象字段由 `notification_event_facts.py` 维护，事件、里程碑与 Resume 结果单向消费。人工待办统一由 `operator_action_facts` 派生，`operator_action_presentation.py` 和 `notification_boundaries.py` 分别渲染查询与卡片；动作通过 `guidance_actions.render_next_action` 渲染，不反向读取 CLI 展示。status/history 的 `operator_actions` 保留逐对象事实；原单项 `operator_action` 是首项投影，不再承担多对象汇总判断。通知的 concise/detailed 选择、occurrence/dedupe、outbox、Ledger 和 transport 仍由原模块维护，查询入口不接入发送过程。

定向 Resume 的 Controller 推进依据实际 PR 读取后保存的 `publication_observed_at`，不从恢复后的等待阶段或排队回执推断。

正式写入中断的展示沿用 [ADR 0022](../adr/0022-publication-write-reconciliation.md)。`multi_ticket.tickets[].pending_write`
投影当前写入意图的操作、分支及 head/base，`holds_integration` 保留未知结果的实际席位。
History 的 `github_write_action` 同时来自 Change Job 的 `ticket_write_intent` 与 Final Run 的 `write_intent`；
发布暂停不表示写入失败或未发生，查询不能清除意图、释放席位或替代远端核验。

Reviewer 的适用结果通过 `review_attempt.finish_review_result` 统一收口；人工暂停记录由 `artifacts.record_human_blocker` 保存。返回结果先持久化，在后继读取与状态同步成功后才清除 pending 并关闭 Attempt；失败恢复不重新调用 Reviewer 或扣预算。候选与 Run 的 currentness、Acceptance Record 和后续决策仍由各自的领域入口负责。

覆盖入口：`test_cli.py` 的机器/人类输出与只读查询，`test_history_*` 的身份、裁剪、转折与语言，`test_status_delivery_actions.py` 的正式/候选验收边界，`test_notification_events.py`、`test_notifications.py`、`test_cli_notifications.py` 和 `test_cli_resume_notifications.py` 的渠道选择、去重、恢复与固定语言。通知共同场景位于 `tests/support/notification.py`；不要从测试用例模块导入该构造器。

三种对象的 CI 恢复共用累计额度投影：`window`、`developments_used`、`development_limit`、
`failure_head`、`review_required`、`reviewer_invocations`、`checkpoint_reason` 与 `history`。
两种 Parent 始终要求 Review，内部 Repair PR 与最终 PR 显示同一 Run 的用量；新失败版本不重置。
Status/History 只读展示当前额度和续期历史，不补造旧内外层计数或执行续期。

审核记录适用性由 `review_evidence` 维护，展示不自行用 tree 或 passed 判断交付权限。
文案通过 `publication_evidence` 核对原 Publish Attempt 的成果与输入边界；文案单独失效
保留适用 Review。CI 与批准分别继续使用准确 PR/head 观测和 `approval_grant` 的原授权对象。
History 的原 Review Context、文案 Attempt 与批准事实不随当前证据失效而改写；
普通 resume/message 不表示批准，待确认的正式写入仍显示原操作并先核验结果。

集成工作中的 `run-integration:<run_id>` 是实际工作身份，`run_acceptance` 是保存位置；人工待办、消息与恢复审计使用实际身份和同一 Generation。Job 暂停阶段与保存的继续阶段分开解释；角色优先读取 pending Attempt，集成结果已保存时进入程序收口，不把它解释为新的开发调用。展示不修改旧历史记录。该解释沿用现有状态字段与协议，不增加旧 Run 迁移或补造缺损材料。

共同执行已保存的 `pending_semantic_attempt.returned_result` 是当前工作事实。成功返回使 `work_role` 投影为程序收口，不再显示需要新的 Agent 调用；Human Blocker/blocked Review 仍显示原角色，明确 resume 清除其返回后继续原 Attempt。展示只读取该事实，不能清除返回、改变预算或放行 currentness。返回缓存不复制到已完成 Attempt 或 Invocation 历史，正式结果继续从原有角色产物取得。

历史阶段事件仍关联实际执行的 Agent：优先从 pending Attempt 读取角色与轮次，Thread 使用其保存的返回或同 Attempt 的 Invocation，再读取角色对应的线程字段。成功返回后的程序收口不会抹去这些历史关联，也不能拿其他角色或其他 Attempt 的线程补齐。
