# 测试支撑的归属与边界

测试模块只提供用例及本文件专用工具。跨文件消费者从 `tests/support/` 导入，
不从 `test_*.py` 或 `conftest.py` 导入。`conftest.py` 负责 pytest fixture 注册与生命周期。
新增共享工具先确认真实消费者，放入对应领域；不要扩大为接受任意 overrides 的通用状态工厂。

| 责任 | 支撑模块 | 使用边界 |
| --- | --- | --- |
| 用户/Git 环境隔离 | `environment` | 每例 HOME/XDG 独立；模板环境使用同一解释器，清除继承的 Git overrides |
| 初始 Run、Fixture GitHub 数据、显式 idle ownership | `seeding` | 调用真实 Controller/StateStore，选项只覆盖现有前置需要；缺失 ownership 的负向测试不要调用 idle seed |
| 真实 CLI、状态读取、内部阶段入口 | `cli` | `run_cli` 保留子进程及环境继承；`run_internal_stage` 仅用于测试准备，不是产品公开命令 |
| 进程内 CLI | `inprocess_cli` | 展示或规则变体；调用结束恢复 cwd、环境和输出流；信号、重启、跨目录选择必须使用真实进程 |
| CLI Agent payload 与完整流程 fixture | `cli_payloads`、`cli_fixtures` | 每次返回新可变数据；不能代替被测持久状态的独立断言 |
| Delivery Agent/Publisher fake | `delivery_agents`、`delivery_publisher` | fake 只控制外部响应；Git、StateStore 和产品状态转换仍真实执行 |
| Run Artifact、Agent 与发布 fake | `run_artifacts`、`run_agents`、`run_publication` | Artifact 数据不反向依赖场景 builder；故障注入保持在消费它的场景 |
| 已完成 Ticket / accepted Integration / accepted Run | `run_scenarios` | 创建真实 commit/ref，Integration 的 parents/tree 与 Git 一致，持久化后供 Acceptance/Repair/Publication 消费 |
| repairing Run 起点 | `repairing_run` | 调用方显式提供 repair trigger；复制 policy/request，保存 StateStore；Candidate 和 currentness 由被测 Engine 执行 |
| CLI 所需 accepted/published Run | `published_run` | 在场景上登记真实 Workspace/Locator/idle ownership；保留历史 Ticket PR 身份，避免 Final PR 复用编号 |
| 控制与恢复 | `control`、`receipt_host`、`lifecycle`、`executor`、`resume`、`resume_intent`、`cli_actions` | 按控制边界选择 fake，不把不同 receipt/退出语义合并为通用 fake |
| 合并冲突与监督 | `merge_resolution`、`merge_faults`、`cli_supervision` | 保留真实 Git 冲突、子进程、信号及归属清理 |

## 合法前置与负向输入

`run_scenarios._completed_run` 创建单个 completed Ticket，其 reviewed Candidate、Integration、
PR merge identity 与 Run ref 对齐；`_accepted_run` 在这个基础上运行真实 Run Acceptance。
两者使用调用方独立仓库与 StateStore，不缓存可变 Run、Locator 或工作目录。
`test_run_scenarios.py` 从落盘状态与 Git 独立读取验证 parents/tree/ref 和 currentness。
`prepare_run_repair` 只提供 Candidate 创建前的有限前置，不校正任意输入。
缺字段、矛盾身份、stale 证据等负向用例继续直接修改原始状态。

唯一共享 Git 模板按 worker 创建，完成写入并关闭自动维护后才返回；每例完整复制。
测试通过或失败都由持有资源的 fixture/调用方负责回收，不能借共享支撑延长 monkeypatch
或可变状态的生命周期。隔离回归见 `test_test_environment.py`；场景测试需能单独及有限并行运行。

## 选择测试层级

先说明独有风险，再决定是否降低成本：展示组合优先调用展示入口；辅助查询可用进程内 CLI；
CLI 启动、环境继承、信号、崩溃恢复、Git、权限、安装和副作用幂等继续保留真实边界。
没有等效覆盖证据时保留现有层级，不能仅凭耗时删除断言。
完整测量与资源口径见[测试入口](test-commands.md#ci-对齐与诊断)。
