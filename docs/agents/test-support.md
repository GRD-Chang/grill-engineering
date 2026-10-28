# 测试支撑的归属与边界

测试模块提供用例及本文件专用工具。共享工具从 `tests/support/` 导入，
不从 `test_*.py` 或 `conftest.py` 导入；`conftest.py` 负责 fixture、收集钩子与安装校验。
已有跨测试依赖按实际消费者逐批迁接，不把旧用例改为兼容转发层。
本页描述当前 Core/SQLite 架构，不再使用已退役的 StateStore、Engine 或旧 Run builder。

| 责任 | 当前支撑模块 | 使用边界 |
| --- | --- | --- |
| 用户与 Git 环境隔离 | `environment`、`workspace` | 每例独立 HOME/XDG、目录和环境；不可变 Git 模板按 worker 创建，每例复制后独立修改 |
| 准备、准入与脚本化 Agent 返回 | `core_operations`、`agent_continuation` | `Ports` / `setup` 保留真实 Core 选择和有限依赖场景；驱动准备→准入→Invocation，不跳过凭据准备。`host_kernel` / `task_run` / `delivery_run` 仅创建有限 Run，`source_operations` / `next_call` 保留原只读事实和正常角色选择；`settle_agent` 仅为原单元场景的显式合成进程观察，不是 OS/Host 退出证明 |
| 公共查询、Profile 与 Doctor 前置 | `cli_fixtures`、`environment` | `_create` / `_published` / `_published_profile_task` 保留正常 Core 创建、准确 Task 绑定及冻结配置；`_file_snapshot` 保留原文件字节视图，不与目录级 `durable_snapshot` 混用 |
| 合成交付世界 | `offline`、`offline_api`、`offline_git` | `Scenario` / `GraphScenario` 使用真实 Kernel、SQLite、私有 Git；Agent 输出和 GitHub 原始对象是合成输入，不是预期结果权威 |
| 原生 Worker 与续接前置 | `agent_continuation`、`native_runtime` | `_allocated_worker` 保留正常准备、源观察与原 RAW；`local_codex` 准备原本地脚本/fixture credential，再调用唯一 `execute_codex`。文件生成检查不等于 Worker/沙箱执行或退出证明 |
| 有限需求场景 | `requirements_scenarios` | `native_input` 每次读取新数据；`independent_source` 仅移除声明的依赖，`source_run` 通过真实 Core 创建库，不补造完成状态 |
| 有限托管控制与观察前置 | `control_scenarios` | `prepared_control` / `managed_approval` 准备准确 PR、合成 manager/HostObservation 及控制观察；`admit_scenario` / `sent_workspaces` 保留原准入和 unknown-intent。`restorable_public_resume` 只准备原隔离损坏输入和已安装 CLI 命令，恢复断言留在用例。调用方拥有控制、读回与退出断言，不把合成事实当 OS 证明 |
| Abandon 与 PR 退休前置 | `control_scenarios` | `managed_abandon_scenario`、`_parent_requeue`、`_retirement` 保留各自原控制顺序、原意图和准确回执；不合并不同恢复权限 |
| 资源清理前置与本地 ref 读取 | `cleanup_scenarios` | `_native_scenario`、`_local_head` 保留原种类和准确 ref 语义；`_intent` / `_checkout` 保留原机械 adapter 场景的准确 REF；`prepared_generation_two_cleanup` 只准备正常 Requeue 与原观察回调，执行和 foreign/退出断言仍由各用例拥有 |
| 真实 gh 子进程与远端 effect 前置 | `remote_effects` | 唯一 `FAKE_GH` 脚本、显式 `fake_gh(repository=..., actor=...)` 字段绑定及 `_fixture`、`_change`、`_pr_effect`、`_publish_pr`、`_source_kernel`、`_reader`、`publication`、`_actual_publication_push`；调用方拥有独立仓库、状态、日志及 monkeypatch |
| GitHub 读取与文本 fixture | `github_requirements`、`github_checks`、`fixture_text` | 保留编号/仓库/父子关系、分页、检查身份及文本路径语义；错误响应仍由真实 reader 拒绝 |
| 写前边界与外部观察 | `effect_boundary`、`api_observation` | 在原外部 I/O 窗口注入变化，保留 prepare、物理核验和最终 Core 准入；`workspace_intent` / `advance_to_effect` 只挂起正常选择出的准确 Effect，不缓存已通过的许可 |
| 受控 manager、托管入口与 Status 端口 | `systemd_manager`、`status_comments` | `_local_manager` 仅替换外部 manager，执行原封存 argv；`_control_environment` / 批准屏障保留原 Git、状态、FD-store 与收尾。manager/sysfs/评论响应的合成输入不冒充真实 OS 或 GitHub 验收 |
| 通知场景、投影与观察 | `notification`、`observer_errors` | `_kernel` / `_operations` 保留有限的冻结配置与只读端口；`_project` 只读实际 history 并调用产品投影；`_enabled_scenario` 保留正常 Core/Git 前置。`_NoNotifications` 仅用于原明确禁发路径；每例持有发送器，关闭后独立检查观察异常，不能依赖被后台吞掉的 assert |
| SQLite 故障 | `sqlite_faults`、`sqlite_fault_host` | 仅精确私有 DB/Run/Action；真实事务故障和清理保持，封存前选择的测试 Host 入口不是官方原 argv E2E |
| 安装及运行库身份 | `installed_runtime`、`offline_install`、`fast_runner_installer`、`native_runtime` | `offline_install._source_tree` / `_fake_codex` / `_run` 保留原离线准备、显式模拟程序与 shell 调用，不等于真实模型或安装激活验收。验证非 editable 安装及源码字节；安装 fixture 使用本地固定依赖，不读取真实账号或在 pytest 内联网准备依赖 |
| 进程及同步 | `process_scope`、`readiness`、`worker_process_timing`、`worker_sigint_probe` | 只管理明确归属的进程/句柄；`guardian_timeout_owners` 保留准确 receipt/pidfd 收割与独立子进程的嵌套 finally。先断言真实退出再执行失败兜底；屏障有界且失败时释放 |

## 有限合法前置与负向输入

`Scenario` / `GraphScenario` 通过正常 Core 操作准备自身交付，真实 Git commit、ref、
parents/tree、需求和 Generation 保持一致。场景之间不共享可变 Kernel、Run、Locator
或工作目录；增加新选项前先确认真实消费者，不建立任意 overrides 的通用工厂。

`CoreOperations` 选择工作并执行相应边界；脚本化 Agent 支撑提供本例声明的外部返回。
准备席位、Invocation、实际执行、退出证明与计费仍由产品负责。
缺字段、跨对象身份、损坏、unknown 和外部漂移测试保留原始负向输入，
不能通过 builder 自动修正为合法状态，也不能直接 SQL 造 completed 前置。

`remote_effects._fixture` 每次创建独立 bare Git、工作仓库、gh 可执行文件及 JSON/调用日志。
`fake_gh` 只绑定该脚本声明的仓库、actor、endpoint 与邮件字符串字段；不串联全局
替换，不再次搜索已插入的值，不改标识符或无关文案。默认程序字节保持，受控扩展
继续使用同一脚本，不新增模拟服务。Completion 事件筛选也复用通知 `_project` 的
准确 Run 读取；同库兄弟 Run 的 history 不能作为当前事件输入。
`_source_kernel` 创建正常 Requirements 与 RunSpec；`_publish_pr` 仍实际执行 push/PR
并检查返回，没有将其改为写入预先完成的业务记录。脚本只合成外部协议，
原 Git、锁、CAS、逐写授权、状态读写、日志、失回复与失败退出继续执行。
脚本所需支撑目录随本轮 checkout 绑定，真实子进程不导入用例模块。

## 生命周期与独立断言

共享支撑不扩大 monkeypatch、fixture、凭据或可写状态的生命周期。
准备不可变模板前结束所有后台写入；Git 模板关闭局部自动维护后才可复制。
并发用例先固定本例环境、准确 Run 和路径，再启动线程或子进程；不要在活动线程间
临时替换进程全局 cwd、环境和标准流。

成功路径先确认线程/进程自然退出和资源关闭；失败路径才有界取消、释放屏障并准确收割。
无 PID、Future 完成或发出信号不等于物理退出证明；清理不得制造业务成功，
不得扫描并回收无关 Run 或其他用例的新进程。原异常为主，清理故障另记。
资源合同由调用方用原始 Git、Core、SQLite、pidfd/退出状态或外部请求日志独立核验，
不把 fake 的分类或同一写操作返回当作完成的唯一证明。

## 验证与迁接

现行代表入口包括：`test_kernel_remote_effects.py` 的 CAS、消息/身份及失回复对账；
`test_kernel_operation_runner.py`、`test_kernel_requirements.py` 的选择、准备和图调度；
`test_kernel_execution.py`、`test_kernel_host_observation.py`、
`test_kernel_managed_entry_end_to_end.py` 的实际进程、Host 和公开恢复；
`test_kernel_query_cli.py` 的公开只读查询；`test_sqlite_fault_scope.py` 与
`test_kernel_sqlite_faults.py` 的故障归属、回滚及恢复。
物理流程必须在具备原系统能力的环境执行，静态迁接不冒充该证据。

每批保留迁移前后的函数/断言、nodeid 集合与选测结果，说明剩余跨测试依赖，
不因一个领域收口就宣称 #294 完成。直接调用其他测试函数的旧消费者仍需另批拆出
正常场景前置，不能仅改名导入或删除其断言。本批没有降低测试层级或性能期限。
完整测量、资源环境和禁跑约束见[测试指南](testing.md)与[测试入口](test-commands.md)。

共享消费者必须沿同一明确 Run 读取：通知 `_project` 使用产品 `event_state` 与准确
Run 的历史，不把全库第一行当作当前 Run；`observed_call` 只读原准备席位所属 Run 的
Invocation/output 复合关系，不负责补准备、Go、活动、raw 或退出事实。
`core_operations.budgets` 沿同一事务的准确 Run 读取当前窗口；不能按 subject/purpose
折叠全库行，metadata 视图也不能自行选 Run。CI Review 的原材料与 Attempt 观察
同样带原 Run，保留原预算字段，不另建预算快照或更新权威。
纯 Core/Git 场景的 `prepare_scripted_agent` 显式代表受控 issuer 已就绪，调用原
`guard_preparation` / `admit_agent`；不为每次调用重复创建不会交给 Worker 的 gh 文件和
续期线程，也不生成 Thread 绑定、Go、活动、raw、退出或费用。真实凭据交付与
`Capture` 测试继续接收 `AgentPreparation`，不得把该受控入口作为真实启动证明。
这些责任由同库兄弟 Task/Run 的受控事务回归验证，不重复启动真实 Host。
没有当前消费者、仅调用已删除 API 的旧 `executor`、`systemd_exit_manager` 和
`durable_save_boundaries` 支撑已退役；历史研究不构成恢复旧协议的理由。
