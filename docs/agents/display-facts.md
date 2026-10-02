# Delivery Run 的只读展示事实

Status、History、CLI 和 Notification 从既有 Run state 派生事实，不另存展示状态，不执行恢复、发送或 Publisher 操作。渠道自行选择字段、摘要和固定 Run 语言下的文案；原始结果及证据保持原文。

| 规则 | 维护归属 | 消费者 |
| --- | --- | --- |
| 角色族、当前 Work Subject、Candidate/PR 诊断身份、Ticket 进度 | `delivery_facts.py` | Status、CLI、History、通知、State 查询 |
| 全部 Operator Gate 对象枚举；对象、Generation、原始证据、局部/全局范围及结构化恢复动作 | `operator_gate.py` / `operator_action_facts.py` | Status、History、操作指引、通知 |
| 按真实 Attempt ID 去重、pending 选择、机器 JSON 字段筛选 | `attempt_projection.py` | CLI、通知、Status 计时 |
| 当前候选验收、发布边界、未解决 Findings | `acceptance_facts.py` | Status、通知；History 共用 Findings 提取 |
| Attempt/Invocation 关联、历史转折、生命周期聚合 | `history_facts.py` | History、通知、Status 计时、Ticket effort |
| 预算快照、准确 Reviewer/候选及支持证据匹配 | `attempt_evidence.py` | History 事实及机器历史投影 |
| Invocation 活性、可信执行时间、恢复等待扣除、Run 历时及完整性汇总 | `execution_timing.py` | Status、History、通知、Ticket effort |

`delivery_history.py` 保留人类布局、详情、显示标签及原有机器历史视图的显式字段筛选；语义记录从 `history_facts.history_records` 取得，再添加渠道文案。事实层不从 CLI、Status 或 History renderer 获取数据。不能以当前 Run 状态补造已裁剪的历史、结束时间、预算或验收结论。

通知的事件外壳、稳定 ID 和工作对象字段由 `notification_event_facts.py` 维护，事件、里程碑与 Resume 结果单向消费。人工待办统一由 `operator_action_facts` 派生，`operator_action_presentation.py` 和 `notification_boundaries.py` 分别渲染查询与卡片；动作通过 `guidance_actions.render_next_action` 渲染，不反向读取 CLI 展示。status/history 的 `operator_actions` 保留逐对象事实；原单项 `operator_action` 是首项投影，不再承担多对象汇总判断。通知的 concise/detailed 选择、occurrence/dedupe、outbox、Ledger 和 transport 仍由原模块维护，查询入口不接入发送过程。

Reviewer 的适用结果通过 `review_attempt.finish_review_result` 统一收口；人工暂停记录由 `artifacts.record_human_blocker` 保存。返回结果先持久化，在后继读取与状态同步成功后才清除 pending 并关闭 Attempt；失败恢复不重新调用 Reviewer 或扣预算。候选与 Run 的 currentness、Acceptance Record 和后续决策仍由各自的领域入口负责。

覆盖入口：`test_cli.py` 的机器/人类输出与只读查询，`test_history_*` 的身份、裁剪、转折与语言，`test_status_delivery_actions.py` 的正式/候选验收边界，`test_notification_events.py`、`test_notifications.py`、`test_cli_notifications.py` 和 `test_cli_resume_notifications.py` 的渠道选择、去重、恢复与固定语言。通知共同场景位于 `tests/support/notification.py`；不要从测试用例模块导入该构造器。
