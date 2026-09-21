# Delivery Run 的只读展示事实

Status、History、CLI 和 Notification 从既有 Run state 派生事实，不另存展示状态，不执行恢复、发送或 Publisher 操作。渠道自行选择字段、摘要和固定 Run 语言下的文案；原始结果及证据保持原文。

| 规则 | 维护归属 | 消费者 |
| --- | --- | --- |
| 角色族、当前 Work Subject、Candidate/PR 诊断身份、Ticket 进度 | `delivery_facts.py` | Status、CLI、History、通知、State 查询 |
| 按真实 Attempt ID 去重、pending 选择、机器 JSON 字段筛选 | `attempt_projection.py` | CLI、通知、Status 计时 |
| 当前候选验收、发布边界、未解决 Findings | `acceptance_facts.py` | Status、通知；History 共用 Findings 提取 |
| Attempt/Invocation 关联、历史转折、生命周期聚合 | `history_facts.py` | History、通知、Status 计时、Ticket effort |
| 预算快照、准确 Reviewer/候选及支持证据匹配 | `attempt_evidence.py` | History 事实及机器历史投影 |
| Invocation 活性、可信执行时间、恢复等待扣除、Run 历时及完整性汇总 | `execution_timing.py` | Status、History、通知、Ticket effort |

`delivery_history.py` 保留人类布局、详情、显示标签及原有机器历史视图的显式字段筛选；语义记录从 `history_facts.history_records` 取得，再添加渠道文案。事实层不从 CLI、Status 或 History renderer 获取数据。不能以当前 Run 状态补造已裁剪的历史、结束时间、预算或验收结论。

通知的事件外壳、稳定 ID 和工作对象字段由 `notification_event_facts.py` 维护，事件、里程碑与 Resume 结果单向消费。共同恢复提示由 `operator_action_presentation.py` 组装，不经过 CLI。通知的 concise/detailed 选择、occurrence/dedupe、outbox、Ledger 和 transport 仍由原模块维护，查询入口不接入发送过程。

覆盖入口：`test_cli.py` 的机器/人类输出与只读查询，`test_history_*` 的身份、裁剪、转折与语言，`test_status_delivery_actions.py` 的正式/候选验收边界，`test_notification_events.py`、`test_notifications.py`、`test_cli_notifications.py` 和 `test_cli_resume_notifications.py` 的渠道选择、去重、恢复与固定语言。通知共同场景位于 `tests/support/notification.py`；不要从测试用例模块导入该构造器。
