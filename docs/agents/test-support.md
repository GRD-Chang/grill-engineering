# 测试支撑的归属与边界

测试模块提供用例及本文件专用工具。共享工具从 `tests/support/` 导入，
不从 `test_*.py` 或 `conftest.py` 导入；`conftest.py` 负责 fixture、收集钩子与安装校验。
已有跨测试依赖按实际消费者逐批迁接，不把旧用例改为兼容转发层。
本页描述当前 Core/SQLite 架构，不再使用已退役的 StateStore、Engine 或旧 Run builder。

| 责任 | 当前支撑模块 | 使用边界 |
| --- | --- | --- |
| 用户与 Git 环境隔离 | `environment`、`workspace` | 每例独立 HOME/XDG、目录和环境；不可变 Git 模板按 worker 创建，每例复制后独立修改 |
| 准备、准入与脚本化 Agent 返回 | `core_operations`、`agent_continuation` | 驱动真实 Core 的准备→准入→Invocation；只读端口不能执行物理写，不能伪造凭据就绪或跳过准备 |
| 合成交付世界 | `offline`、`offline_api`、`offline_git` | `Scenario` / `GraphScenario` 使用真实 Kernel、SQLite、私有 Git；Agent 输出和 GitHub 原始对象是合成输入，不是预期结果权威 |
| 有限需求场景 | `requirements_scenarios` | `native_input` 每次读取新数据；`independent_source` 仅移除声明的依赖，`source_run` 通过真实 Core 创建库，不补造完成状态 |
| 已发布 Ticket 的控制前置 | `control_scenarios` | `prepared_control` 只准备准确 PR、合成 HostObservation 及控制观察；调用方自行执行 Requeue/Abandon 并保留逐步和读回断言，不调用其他测试函数 |
| 真实 gh 子进程与远端 effect 前置 | `remote_effects` | 唯一 `FAKE_GH` 脚本及 `_fixture`、`_change`、`_pr_effect`、`_publish_pr`、`_source_kernel`；调用方拥有独立仓库、状态、日志及 monkeypatch |
| GitHub 读取与文本 fixture | `github_requirements`、`github_checks`、`fixture_text` | 保留编号/仓库/父子关系、分页、检查身份及文本路径语义；错误响应仍由真实 reader 拒绝 |
| 写前边界与外部观察 | `effect_boundary`、`api_observation` | 在原外部 I/O 窗口注入变化，保留 prepare、物理核验和最终 Core 准入；不缓存已通过的许可 |
| 合成 systemd 与 Status 端口 | `systemd_manager`、`systemd_exit_manager`、`status_comments` | 只提供声明的外部 manager、sysfs 或评论响应；不将合成观察冒充真实 OS 或 GitHub 验收 |
| 通知观察 | `notification`、`observer_errors` | 启动、关闭和异常归属由每例持有；发送器关闭后独立检查观察异常，不能依赖被后台吞掉的 assert |
| SQLite 故障 | `sqlite_faults`、`sqlite_fault_host` | 仅精确私有 DB/Run/Action；真实事务故障和清理保持，封存前选择的测试 Host 入口不是官方原 argv E2E |
| 安装及运行库身份 | `installed_runtime`、`offline_install`、`fast_runner_installer`、`native_runtime` | 验证非 editable 安装及源码字节；安装 fixture 使用本地固定依赖，不读取真实账号或在 pytest 内联网准备依赖 |
| 进程及同步 | `process_scope`、`readiness`、`worker_process_timing`、`worker_sigint_probe` | 只管理明确归属的进程/句柄，先断言真实退出再执行失败兜底；屏障有界且失败时释放 |

## 有限合法前置与负向输入

`Scenario` / `GraphScenario` 通过正常 Core 操作准备自身交付，真实 Git commit、ref、
parents/tree、需求和 Generation 保持一致。场景之间不共享可变 Kernel、Run、Locator
或工作目录；增加新选项前先确认真实消费者，不建立任意 overrides 的通用工厂。

`CoreOperations` 选择工作并执行相应边界；脚本化 Agent 支撑提供本例声明的外部返回。
准备席位、Invocation、实际执行、退出证明与计费仍由产品负责。
缺字段、跨对象身份、损坏、unknown 和外部漂移测试保留原始负向输入，
不能通过 builder 自动修正为合法状态，也不能直接 SQL 造 completed 前置。

`remote_effects._fixture` 每次创建独立 bare Git、工作仓库、gh 可执行文件及 JSON/调用日志。
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
