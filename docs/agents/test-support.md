# 测试支撑的归属与边界

测试模块只提供用例及本文件专用工具。跨文件消费者从 `tests/support/` 导入，
不从 `test_*.py` 或 `conftest.py` 导入。`conftest.py` 负责 pytest fixture 注册与生命周期。
新增共享工具先确认真实消费者，放入对应领域；不要扩大为接受任意 overrides 的通用状态工厂。

| 责任 | 支撑模块 | 使用边界 |
| --- | --- | --- |
| 用户/Git 环境隔离 | `environment` | 每例 HOME/XDG 独立；模板环境使用同一解释器，清除继承的 Git overrides |
| 初始 Run、Fixture GitHub 数据、显式 idle ownership | `seeding` | 调用真实 Controller/StateStore，选项只覆盖现有前置需要；缺失 ownership 的负向测试不要调用 idle seed |
| 真实 CLI、状态读取、内部阶段入口 | `cli` | `run_cli` 保留子进程及环境继承；`run_internal_stage` 仅用于测试准备，不是产品公开命令 |
| Run 查询、持久逻辑快照与原始负向输入 | `state` | 通过真实 StateStore 读取；原有故障注入与精确 history fixture 用 SQLite repository 写入，首次创建使用 StateStore；已有场景准备使用 persist_run_fixture，业务动作使用对象入口，不模拟 JSON 文件布局 |
| 进程内 CLI | `inprocess_cli` | 展示或规则变体；调用结束恢复 cwd、环境和输出流；信号、重启、跨目录选择必须使用真实进程 |
| CLI Agent payload 与完整流程 fixture | `cli_payloads`、`cli_fixtures` | 每次返回新可变数据；不能代替被测持久状态的独立断言 |
| Delivery Agent/Publisher fake | `delivery_agents`、`delivery_publisher` | fake 只控制外部响应；Git、StateStore 和产品状态转换仍真实执行 |
| Run Artifact、Agent 与发布 fake | `run_artifacts`、`run_agents`、`run_publication` | Artifact 数据不反向依赖场景 builder；故障注入保持在消费它的场景 |
| 已完成 Ticket / accepted Integration / accepted Run | `run_scenarios` | 创建真实 commit/ref，Integration 的 parents/tree 与 Git 一致，持久化后供 Acceptance/Repair/Publication 消费 |
| repairing Run 起点 | `repairing_run` | 调用方显式提供 repair trigger；复制 policy/request，以显式 fixture 设置已有场景；Candidate 和 currentness 由被测 Engine 执行 |
| CLI 所需 completed/accepted/published Run | `published_run` | 在场景上登记真实 Workspace/Locator/idle ownership；保留历史 Ticket PR 身份，避免 Final PR 复用编号；completed 起点不预先执行 Run Acceptance |
| Ticket 首次 Required Checks | `ticket_scenarios` | 真实 Candidate/Publication、审核授权与 SQLite；尚未建 PR 或首次读 checks，后续由 Engine 推进，不伪造调用历史 |
| CLI 已审核、尚未 Publication 的 Ticket | `accepted_ticket` | 仅单票 #2/#3；真实 Workspace/Profile/Locator/idle ownership 及开发入口的 base refs；Candidate 尚未发布，Publication、故障和恢复仍由真实 CLI 执行 |
| Parent/Ticket/Final Run 的展示结果 | `cli_delivery_display` | 真实 Git、PR 与对象保存；展示入口持久化 intent 后调用外部展示，逐组合重开核对并验证不重复执行 |
| Parent-only 审批规则 | `parent_scenarios` | 真实 Git、审核身份、PR 与 idle ownership；历史额度不冒充真实 Invocation，不用于依赖 Publication Attempt 的故障对账 |
| Run 受阻审核通知 | `notification_scenarios` | 从已完成 Ticket 起点执行真实 Reviewer、Profile、通知查询及 observer；关闭本次 sender，后续恢复仍走 CLI |
| 控制与恢复 | `control`、`receipt_host`、`lifecycle`、`executor`、`resume`、`resume_intent`、`cli_actions` | 按控制边界选择 fake，不把不同 receipt/退出语义合并为通用 fake |
| 受控后台交付 | `background_delivery` | 共享真实 Executor 取消上下文；失败时关闭准入、释放阶段屏障并有界回收交付线程，再结束所有权占位进程和通知；业务完成由用例独立确认 |
| 并发测试的线程生命周期 | `ticket_execution` | 原窗口内先断言正常退出；失败时独立取消并释放 gate，等待本次线程池退出后才能关闭 sender 或撤销 fixture；取消不能制造成功 |
| 共享存储故障的真实 Worker | `shared_storage`、`shared_storage_processes` | 历史准备先于 ready 计时；测试资源登记独立于受测故障存储，按 PID/start token 回收全部启动者，使用 subreaper 回收孤儿并保护无关进程 |
| 合并冲突与监督 | `merge_resolution`、`merge_faults`、`cli_supervision`、`parent_approval_update`、`run_approval_update` | 保留真实 Git 冲突、子进程、信号及归属清理 |

## 合法前置与负向输入

`run_scenarios._completed_run` 创建单个 completed Ticket，其 reviewed Candidate、Integration、
PR merge identity 与 Run ref 对齐；`_accepted_run` 在这个基础上运行真实 Run Acceptance。
历史 Ticket 保留准确 `ticket_branch` 归属，分支及 checkout 已清理；后续公开恢复可真实对账清理，不必重跑 Ticket 交付。

原始替换 fixture 接收工作投影时，先从同一 Run 补读其明确声明未读的 Timeline，
再清除已展开的范围标记，避免丢失前缀或把完整输入继续标成未读。
这是测试准备中的完整化，不用于测量日常保存；没有声明未读范围的缺失或损坏字段
继续保留原故障含义。需要删除历史字段或复制成另一个 Run 时，先显式读取完整状态。
该边界由 `test_state_fixture_timelines.py` 验证，生产保存仍拒绝覆盖未读历史。
两者使用调用方独立仓库与 StateStore，不缓存可变 Run、Locator 或工作目录。
`test_run_scenarios.py` 从落盘状态与 Git 独立读取验证 parents/tree/ref 和 currentness。
`prepare_run_repair` 只提供 Candidate 创建前的有限前置，不校正任意输入。
缺字段、矛盾身份、stale 证据等负向用例继续直接修改原始状态。

Ticket/Parent 的审批与 checks 场景只准备明确的历史事实，不补造 Invocation 或
Semantic Attempt。`test_ticket_scenarios.py`、`test_parent_scenarios.py` 核对落盘状态、
真实 parents/tree/ref、currentness 及首次 checks。`test_accepted_ticket_scenarios.py`
另核对未发布 Candidate、准确需求、base refs 和 Thread/Profile 归属；该前置
不生成 Invocation 或 Semantic Attempt。关闭响应丢失等依赖已完成 Publication
Attempt 的场景仍执行真实 Publication 前序及恢复，不能以历史累计数替代。
`test_published_run_scenarios.py` 另核对
Locator、Profile 版本、idle ownership 和跨目录公开查询的准确身份及只读边界。
`resume._prepare_terminal_resume_boundary` 通过真实 Engine 与 Control API 准备终止现场，
仅供缺失/损坏 Control 的拒绝矩阵使用；各类公开运行/恢复边界继续保留真实进程代表。

唯一共享 Git 模板按 worker 创建，完成写入并关闭自动维护后才返回；每例完整复制。
测试通过或失败都由持有资源的 fixture/调用方负责回收，不能借共享支撑延长 monkeypatch
或可变状态的生命周期。隔离回归见 `test_test_environment.py`；场景测试需能单独及有限并行运行。

并发断言只涉及 Ticket frontier 时，`ticket_execution.deliver_through_ticket_frontier`
保留真实 Driver 的监督和图刷新回调，仅在真实 `develop_frontier` 已返回、线程池已退出后结束本次调用。
若场景还断言全票完成，应随后同步运行真实交付；目标本身要求的在途 Publication 不提前终止。
失败取消只负责资源回收，不能替代正常退出断言。

## 选择测试层级

先说明独有风险，再决定是否降低成本：展示组合优先调用展示入口；辅助查询可用进程内 CLI；
CLI 启动、环境继承、信号、崩溃恢复、Git、权限、安装和副作用幂等继续保留真实边界。
没有等效覆盖证据时保留现有层级，不能仅凭耗时删除断言。
完整测量与资源口径见[测试入口](test-commands.md#ci-对齐与诊断)。
