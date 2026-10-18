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

## 通知观察与后台错误

`workspace.managed_workspace/managed_state/managed_repo` 从显式环境映射计算路径，
不临时覆盖进程环境。存在活动线程时，先准备准确的 StateStore、Run ID、Git 路径
和子进程环境，再开始交错；需要不同环境的 Workspace 创建在独立进程完成。
`inprocess_cli` 会改变 cwd、环境和输出流，只能在这些全局资源没有其他活动消费者时使用；
Worker 内的辅助查询使用真实 `run_cli` 和显式环境。

业务通知会捕获观察和传输异常以继续交付，测试不能只在回调内 assert。
`observer_errors.ObserverErrors` 保存原始观察异常；调用方在 sender 关闭后独立检查。
真实 CLI 仪器由 `notification_cli` 在 Executor 的 close 边界重新抛出观察错误，
不能仅检查 fork 前主进程中的错误列表。正常路径先确认线程和进程退出，失败路径
才使用有界取消；清理异常作为原始错误的 note，不让取消替代业务完成断言。

回归入口：`test_workspace_concurrency.py`、`test_notification_observer_errors.py`、
`test_notification_cli_support.py`、`test_ticket_execution_cleanup.py`、`test_ticket_process.py`。
批准更新的真实 SQLite/Git 交错仍由
`test_run_approval_update.py::test_run_update_reviews_latest_target_after_another_append`
验证；语言与三轮投入字段组合由 `test_notification_display.py` 验证，真实通知传输、
顺序、慢传输、取消和 run/approve 继续由 `test_cli_notifications.py` 保留。

`TicketExecution` 与 `background_delivery` 的真实并发消费者使用
`ticket_process.isolated_ticket_test` 在独立解释器保留完整测试断言；Python 无法安全强杀
任意线程，线程取消超时或 release 抛错时，由该测试子进程退出/有界强制回收停止
剩余线程和后代，父进程确认退出后才撤销 fixture。它只覆盖这些支撑的直接消费者，
不为全库增加运行器或自动重试。负向回归同时保护在隔离前已存在的无关进程。
通知 CLI 在启动前固定 Task/StateStore，仅补读该 Task 新增的 Action/Generation 及
Executor PID/start token；失败回收只冻结并回收已确认拥有的进程和后代。
不扫描其他任务的 Control，也不把 pytest 新出现的全部后代推断成本例进程。
负向回归让其他 Task 在本次 CLI 启动后再创建子进程，确认两者均不受影响。

隔离支撑只接受模块顶层函数及当前消费者的 Path、MonkeyPatch、JSON 参数。
父 fixture 准备文件和环境；函数内 patch 在子进程执行，不复制父解释器的可变模块状态。
子进程通过独立 socket 报告原始异常，输出单独捕获，后代继承输出 fd 不会阻塞失败回收。
正常返回仍有活动线程/进程，或未捕获的线程异常，都使主测试失败。
Python 3.12 的多线程 fork 警告在该子进程中按错误处理，保持并行验证可见。

每次调用由轻量监督解释器先确认 Linux subreaper 能力，再启动实际测试解释器。
测试解释器即使 `os._exit` 或被 SIGKILL，已切换 session 的后代仍收养到本次监督进程；
后者报告结果后保持存活，直到调用方按其准确进程树有界回收。不依赖测试结束前的
一次性 owned 快照，也不将 pytest 的其他新后代认成本例资源。监督层按测试解释器退出
判断硬退出，不能只等可能被 raw fork 后代继承的 socket EOF。
子测试原始 traceback 优先保留；接收、退出及清理错误附加到原错，即使仅有清理故障，
也必须明确失败。输出回传和资源关闭仍继续尝试，fixture 撤销前完成回收。

## Final Run PR 身份覆盖

`published_run.FinalRunContext` 只携带每例 Workspace、Git/StateStore 引用、准确 Run ID、
fixture 路径和启动前捕获的子进程环境；`prepare_accepted_context` 与
`prepare_published_context` 分别停在 Final Publication 前与显式批准前。
后续准备不再次 attach Run 或发现状态根。业务动作后用原 StateStore 的准确 Run ID
重新读取，拒绝后的断言使用独立落盘事实；生产重开和每次 Git 调度仍重新校验授权。
旧的 tuple 返回接口继续供其他场景使用，不推广到其他领域。

身份字段组合在 `test_run_pr_identity.py::test_identity_fields` 验证现有生产身份判定：
head/base repository、head ref/SHA、base ref/SHA、repository 与 ref 同时错误及合法对照。
三类应用入口仍真实运行 Engine，并另保留独立 CLI 进程；fake 仅控制外部响应。

下表中的旧 nodeid 均以 `tests/test_cli_delivery.py::` 开头。

| 旧 nodeid / 故障窗口 | 当前覆盖 |
| --- | --- |
| `test_final_run_pr_drift_returns_to_fresh_acceptance_without_rewriting_pr[head/base/head-repository/head-ref]` | `test_run_pr_identity.py` 中保留原函数名和四个参数地址，通过显式 accepted context 调用真实 Publication Engine，断言 `foreign_run_pr` 与原 PR title/body 不被改写；CLI 错误分类由下述恢复代表覆盖 |
| `test_persisted_final_run_pr_recovery_rejects_foreign_identity_without_effects[head-repository/head-ref]` | `test_identity_application_rejects_before_unauthorized_effects[recovery-repository/recovery-ref]` 保留两组应用组合，完整逻辑快照与全部外部 delivery 不变；`test_cli_run_pr_identity.py` 中的同名无参数 CLI 代表保留退出码、持久阻断、Invocation、status/history 与准确 Action receipt |
| `test_explicit_approval_rejects_foreign_final_run_pr_identity_without_external_writes[head-repository/head-ref]` | 应用组合 `[approve-repository/approve-ref]`，除准确 grant 及对应审核事件外比较完整落盘事实；`test_cli_run_pr_identity.py` 中的同名无参数 CLI 代表保留 receipt/grant、Invocation 和全部外部 delivery 不变 |
| `test_post_merge_readback_rejects_foreign_identity_before_parent_closeout[head-repository/head-ref/base-repository]` | 应用组合 `[readback-head-repository/readback-ref/readback-base-repository]` 保留真实 merge parents、一次 merge intent 与外部调用记录；`test_cli_run_pr_identity.py` 中的同名无参数 CLI 代表仍证明 PR 已合并、Parent 未关闭和准确 receipt/grant |

`test_run_publication_checks.py` 的 merge 前二次身份校验及 snapshot 漂移继续覆盖检查后变化；
`test_run_publication_reconciliation.py` 保留 merge 响应丢失、重开及 base 推进后的对账。
`test_git_dispatch_fence.py` 保留等待锁后的授权撤销与每次写入重查；
`test_cli_workspace.py`、`test_managed_workspace.py` 保留受管祖先与生产重开校验；
`test_published_run_scenarios.py::test_final_context_does_not_authorize_replaced_workspace`
另在 Context 创建后替换 origin，验证公开 CLI 拒绝且完整 Run/Control 事实不变。
`test_published_run_scenarios.py` 独立读取真实 Git、SQLite、Locator/Profile 与跨目录 CLI 查询。
环境回归在 `test_test_environment.py` 顺序运行独立可变副本并核验模板静止、宿主文件保全；
继承的 `GIT_SHALLOW_FILE`、`GIT_NAMESPACE` 不进入模板和每例准备，生产 Git 也清除外部 shallow
文件覆盖，仓库自身的 `.git/shallow` 边界由 `test_git_bounded_output.py` 保留。
