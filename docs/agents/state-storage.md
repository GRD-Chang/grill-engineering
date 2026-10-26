# Run 状态存储与提交合同

整体模块责任见[当前架构](../architecture.md)。本页描述现行 `Storage`、
`Transaction` 与 Core 保存事实的实现，不定义另一套生命周期或权限策略。

## 路径与唯一支持的协议

数据主目录默认为 `~/.local/share/agent-run/`，或 `$XDG_DATA_HOME/agent-run/`。
每个新 Run 的控制、Host 与业务关系保存在
`repositories/<owner>/<repo>/state/<run_id>/kernel.sqlite3`；
数据库的 `meta.protocol` 必须为 **`independent-delivery-kernel-v21`**。
`native-task-locations/<task-fingerprint>.json` 只保存该 Task 的 Run ID 和数据库路径，
不复制生命周期状态；替换与占用判断仍由 Core 决定。

[`storage.preflight`](../../src/agent_run/storage/preflight.py) 以只读连接检查现有数据库。
旧 JSON/v6、早期 native v17/v18/v19/v20、来源封印及导入状态明确拒绝，
保留原文件、Candidate、工作区和成果，不升级、不转换、不双写，也不直接续接。
[`storage.admission`](../../src/agent_run/storage/admission.py) 只检查旧 Task 占用及拒绝条件，
不解释或转移旧工作。拒绝不授权清除旧 Task、封印或历史数据。

`Storage.create` 排他创建新文件，拒绝接管已有数据库或遗留 sidecar。
写事务在打开写连接前、取得写锁后及提交前复核支持边界；查询同样只接受当前 native 状态。
旧迁移链与 `original_query` 已退出，不存在跨版本查询或原地升级入口。

## 事实的唯一归属

[`storage.sqlite`](../../src/agent_run/storage/sqlite.py) 的 `SCHEMA` 是表和列的实现依据：

| 事实 | 当前关系 |
| --- | --- |
| Run、交付对象及代次 | `runs`、`subjects`、`retired_generations` |
| 固定输入与需求观测 | `run_inputs`、`requirement_versions`、`source_observations`、`profile_revisions` |
| 语义工作与真实调用 | `attempts`、`invocations`、`invocation_outputs`、`threads`、`worker_activity` |
| 预算、调度与暂停 | `budgets`、`work_queue`、`integration_queue`、`leases`、`gates`、`waits` |
| 外部操作与适用证据 | `effects`、`effect_failures`、`checks`、`grants`、`completions` |
| 控制、宿主与审计 | `actions`、`hosts`、`retired_executors`、`history` |
| 有拥有者和用途的原格式材料 | `material` |

业务阶段与 gate 分开；暂停保留继续工作，定向 resume 只解除获准条件，
不能清除兄弟门禁或 Run 全局控制。仍有暂停时不能准入下一段工作，
仅补齐继续阶段不会替换已有的准确外部诊断；拥有者关系不授予执行权限。
普通预算窗口、CI、集成开发及 resync 保留原归属与续期规则。
预算保留用途、窗口、累计用量及原证据，显式续期或同次中断续接不能重复记账。

Attempt、Invocation 和 Thread 各有独立 `id`，通过明确外键关联。
物理输出以 `(invocation, ordinal)` 标识；原始返回、进程身份与活动事实属于准确输出，
不从后来的当前对象重建早先调用或审核依据。Attempt 的 `thread_id` 和 `opening_id`
由 continuation 入口绑定，分别引用 Thread 和不可变 Opening 材料。

凭据准备只在现有 `waits(kind=agent_preparation)` 预留准确 Attempt 的工作席位，
固定绝对截止最多 600 秒；读取、重试和兄弟进度不续期。准备席位与真实 lease
共同占用并发容量。凭据就绪后，同一事务以原 reservation ID 创建 Invocation/output/lease
并删除准备 Wait；执行期限从该准入点开始，Output Repair 仍继承原 Invocation 截止。
准备失败不创建 Invocation、不消耗 Development 用量；同一 Attempt 在最终 Worker
放行准入或实际返回工作证据处仅记账一次。准备取消只有准确资源清理完成或原 Host
退出证明才能释放席位；Future 完成及未知 Host 均不是清理证明。

v20 的分配时扣费且 `accounted=0` 不能解释为 v21 延迟扣费。当前 Runner 在写前、
锁内与读取时明确拒绝 v20；不迁移、不补偿退款、不改写其用量。保留原数据及匹配
Runner Snapshot，以原版本恢复；安装新 Runner 不意味着能够继续旧 Run。

需求/图版本、候选、预期合并树与 Ticket 完成事实保留准确关联，
后来的当前对象不得改写早先审核或批准依据。
明确指定但无法解析的需求/图 revision 必须拒绝，不能降为空引用。
批准、关闭 intent/receipt 及结果未知时的读回边界继续保留。

各角色的原始返回落盘与阶段消费是不同事务边界；重开后优先复用已存结果。
结果消费和适用预算变化由 Core 在同一业务事务收口，重复返回不能再次执行或记账。
原始返回尚未落盘的中断仍属于未知结果，不能假定成功。

`Transaction.put/get` 按拥有者、用途、SHA-256 和实际字节数保存及核验材料。
材料不借用其他对象的证据，不以截断正文、删除历史或重置预算处理缺损和超限。
Final Run PR 的远端内容仍为 PR Narrative 与单条原地更新的 Agent Run Status；
不再发布完整 `Run Publication Record` JSON 评论，也不把该 JSON 搬入正文或另一评论。
历史评论保留原样；缺少该评论不影响待批准、准确合并或恢复。
本地验收、发布、批准、预期合并树和 Required Checks 核验继续有效。
Status 的 `Recorded Review` 只表达与当前记录的候选、base、generation 及需求范围匹配的
Review：已消费结论为 `pass` / `request_changes`，尚未消费为 `pending`；
过期、未知或没有记录分别明确标注，不推测通过，也不发布完整 Artifact。
这是只读展示，不是实时远端复查或合并授权；评论仍使用原绑定和原 intent 对账更新。

## 读取与事务

`Storage.transaction(write=False)` 使用 `read_state` 的 `mode=ro`、`query_only`
及只读事务；`transaction(write=True)` 使用 `mode=rw`、`BEGIN IMMEDIATE`，
事务内失败共同回滚，已被调用者捕获的 SQL 失败也会使该事务失效。
每次边界独立打开并关闭连接，不复用旧 StateStore 的可变 Run 快照或工作作用域缓存。

`Transaction.rows` 在搬入正文前由 SQL 统计列长度并累计读取额度；
`all` 通过物理分页读取集合，`one` 要求唯一事实。材料先检查拥有者、SQL 类型、
实际长度及额度，再读取并核对摘要。现行接口不包含旧 `load_*`、
`sqlite_change_job_queries` 或 `sqlite_query_rows` 模块。

公开查询由 [`queries.run.read`](../../src/agent_run/queries/run.py) 在同一只读快照中组织
status、history 和冻结 settings。history 使用 `after`/`limit`、事件位置和 `has_more`
显式分页；查询不会把部分结果伪装成完整历史，也不会回填业务事实或通知记录。
未读取的历史不能因空列表而被清除，查询结果与显示字段不授予保存或执行权限。
自定义状态目录继续只提供查询，不能启动或恢复执行。

外部操作、Agent 调用和等待均在业务写事务之外。
OperationRunner 将读回证据交给 Core 核验和消费，数据库提交不能替代实际外部结果。
通知观察已提交事实，投递失败不回滚已提交的业务事实；查询不会发送通知。

## 实现中的资源界限

界限来自 `storage.sqlite` 的常量及 Core 对准确记录的检查，不从源码行数或测试总耗时推导。

| 位置 | 当前界限及检查 |
| --- | --- |
| 材料 `MATERIAL_LIMIT` | 单份 4 MiB；核验类型、长度、拥有者和摘要 |
| Core 当前记录 `CURRENT_LIMIT` | 256 KiB；由 Core 的当前事实/操作 token 检查使用 |
| `Transaction.rows` | 每次至多 128 行；同一事务累计至多 65,536 行、32 MiB 读取 |
| 公开响应 | `queries.run` 按实际编码结果检查 32 MiB 上限 |
| 主库 `DATABASE_LIMIT` | 256 MiB；事务边界检查页面数乘页面大小 |
| 库及 sidecar `PHYSICAL_LIMIT` | DB/WAL/SHM 合计 1 GiB；检查实际大小，写事务另在 COMMIT 前预留提交增长 |

这些检查不宣称存在旧的每批 8 MiB 门禁、`max_page_count` 设置或跨事务查询缓存。
超限明确失败，不截断证据，不通过删历史或扩大业务预算回退；
容量界限也不能替代身份、版本、外键、材料摘要、批准适用性与未知结果对账。

写事务持有原 `BEGIN IMMEDIATE` writer lock，在提交前将当前实占用与保守增长预留合并检查：
剩余 WAL 页面帧、标头与 FULL 同步填充，SHM 索引区域及 OS 页对齐，
以及并发 checkpoint 在既有 `DATABASE_LIMIT` 内可能造成的主库增长。
预留不足返回 `storage_limit`，原事务回滚；不在已提交后抛错冒充回滚。
因此写入可能在实际文件尚未达到 1 GiB 时拒绝，不保证可使用到上限的最后一个字节；
配额数值及数据库上限不变，读取只检查实际占用。

这是受管 SQLite 事务的提交准入界限，不是文件系统硬配额：事务体的 cache spill 可能临时增加 WAL，
回滚也不承诺立即缩短 WAL 尾部。正常 checkpoint 可回收 sidecar 空间，
程序不删除原始证据、扩大配置或改变 SQLite 同步、缓存、checkpoint 策略来规避拒绝。

## 控制、原子性与收口权限

控制关系与业务关系同库；生命周期 Action 及相关业务变化在短事务重验并提交，异常共同回滚。
持久工作 FIFO 与集成单席由 Core 维护，不是旧的 Store 写请求排队框架。
Stop/Abandon、冲突拒绝、不可取消批准及集成单席合同不变。
作用域只选择入口，不能替代准确授权。

业务保存通过 `Storage.transaction` 的进程内 FIFO 取得资格。顺序的线性化点是
同一普通数据库文件身份（device/inode）的 ticket 登记；同库不同 `Storage` 实例及
允许的目录别名共用入口，不承诺调用尚未登记时的墙钟顺序，也不承诺跨进程 FIFO。
Worker 的 Thread/活动/原始返回/退出记录，以及 Core 的工作分配、观察消费、
正式写意图与读回保存使用这一入口；完整业务操作仍可能包含若干独立短事务。
ticket 不保存业务正文、权限或待重放闭包，原调用线程取得资格后才打开写连接，
沿原逻辑重读身份、代次、Stop 栅栏、协议和资源界限，事务体只执行一次。

生命周期 Action 的 `requeue`、`admit_host`、`control_host`、`stop_task`、
`abandon_task`、`control`、`configure_profiles`，以及排他初始化 `create` 和
共享故障撤权 `_retire_failed_executor` 显式使用 `ordered=False`。这些控制入口
不等待业务 ticket，因此不会在持有 Task/admission 锁时等待业务保存资格；
它们仍受原 SQLite 写锁、全部校验及原子提交约束，不保证瞬时抢占已开启的事务。
只读查询不排队。工作/集成 FIFO、控制 Action 和保存 ticket 各有既有职责。

资格等待、preflight 和取得 writer lock 共用从写入口开始的 **1 秒**竞争预算，
SQLite 连接只接收剩余时间，不在排队后重新增加一秒；这不是事务体、Agent 或
业务等待窗口的运行期限。每库最多保留 128 个业务 ticket（含当前头部），满额
或到期在事务体执行前返回 `storage_busy`，不自动重放 Agent、SQL 事务或外部动作。
该元数据额度不改变材料、查询、数据库或冻结业务 Policy 的容量限制。

`cancelled` Event 仅撤销尚未进入事务体的保存等待；Stop 不会笼统丢弃应保留的
原始返回和退出收口，它们继续由 Core 分别授权。异常、取消、超时均撤销准确 ticket，
提交/回滚且连接关闭后才释放资格，空闲 registry 项立即移除；fork 子进程重置
进程内协调对象。同线程嵌套写以 `storage_busy` 拒绝，不复用另一笔事务或改变
SQL 错误 poison/rollback 合同。等待期间文件身份改变仍拒绝，不接管替换数据库。

正常推进与在途结果、取消、真实退出收口分别核验。Stop 已生效、租约已释放或
旧 Executor 被替换后，原 Invocation 仍可在准确身份、代次、Thread 与 Attempt
核验后保存结果及退出事实；阶段、预算、Candidate、gate 和队列保持当前值。
该权限不能启动新 Agent、执行外部动作或推进后继阶段。

共享存储持续失效时关闭新准入，并通过本次执行作用域内的取消信号、准确 Worker
和 Host 控制停止执行，不依赖另一笔成功写入，也不新增授权账本。
首次公开 resume 在准入后继前核验旧 Executor 与登记 Worker 的 PID/启动身份和
真实退出，再统一收口该代未完成调用、租约及准确历史。未知或缺失的退出证明
不能当作已退出。已保存返回、Attempt、Thread、代码、预算和局部门禁保持。
未知 Git/GitHub 结果继续沿原 intent/receipt 读回真实事实，不盲目重放。

## SQLite 运行库与资源生命周期

使用 WAL、`synchronous=FULL` 和连接级外键校验。外键在事务开始前开启，
关联更新遵循 SQLite 的[外键约束](https://www.sqlite.org/foreignkeys.html)与
[事务边界](https://www.sqlite.org/lang_transaction.html)，不能把数据库提交当作
外部动作恰好一次执行或宿主退出证明。

Python 实际加载的 SQLite 必须包含 WAL-reset 修复：3.51.3 及以上，或官方回补
分支 3.50.7–3.50.x / 3.44.6–3.44.x。开发与 CI 的固定源码、哈希及动态库准备
见[测试指南](testing.md)。版本检查适用于实际解释器，不修改系统运行库。

连接属于一次事务边界，成功和失败都关闭本次连接；不会缓存跨事务状态事实。
协议调整不授权操作真实旧 Run。Host、Worker、通知及临时材料仍由其实际拥有者收口，
不能把 SQLite 连接关闭或数据库提交当作进程退出证明。
