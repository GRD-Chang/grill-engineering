# Run 状态存储与提交合同

整体模块责任见[当前架构](../architecture.md)。本页描述现行 `Storage`、
`Transaction` 与 Core 保存事实的实现，不定义另一套生命周期或权限策略。

## 路径与唯一支持的协议

数据主目录默认为 `~/.local/share/agent-run/`，或 `$XDG_DATA_HOME/agent-run/`。
本开发候选按受管仓库共用 `repositories/<owner>/<repo>/state/kernel.sqlite3`，
`meta.protocol` 唯一值为 **`independent-delivery-repository-v23`**。
该源码候选仍待同版独立验收，不代表用户环境已经切换或真实进程验收通过。
`tasks` 保存准确Task身份和可空的 `current_run`；`task_runs` 绑定历史Run。
独立idle Task不产生空壳Run、Invocation、预算或执行权。完整冻结Run及必要关系/材料
与Task绑定在同一短事务中提交；COMMIT未知仅对账原proposal，不重新创建或套用新默认值。
创建与打开从 [`runtime.task`](../../src/agent_run/runtime/task.py) 进入：应用层选择
Workspace，`core.task_creation` 使用明确的 Storage 与冻结输入完成绑定，
`core.run_creation` 与 `core.source_snapshot` 保存创建及需求事实。
`RepositoryRoute` 只定位、核验和分页发现；查询文档由 `queries.run.read_route` 组合。
创建失败后的自动读回继续使用原 proposal、实际选中 Run 和原绝对 deadline；
显式对账可读取已经被后继替换的原 Run，缺失原请求证据仍拒绝，不分配新 ID。

[`storage.preflight`](../../src/agent_run/storage/preflight.py) 只读检查原数据库。
旧JSON/v6、native v17–v22、早期仓库实验marker、封印和导入状态均拒绝，
保留原SQLite/WAL、raw、材料、预算和工作区，不升级、不转换、不双写。
旧的每Run库目录残留不能被当作空仓库重新初始化。
Task锁仍使用 `native-task-locations/<task-fingerprint>.lock` 的原inode；
该目录不再发布JSON绑定，残留JSON只能拒绝控制准入，不能授权、定位或覆盖新事实。

`Storage.create` 排他创建新文件，拒绝接管已有数据库或遗留 sidecar。
写事务在打开写连接前、取得写锁后及提交前复核支持边界；查询同样只接受当前 native 状态。
旧迁移链与 `original_query` 已退出，不存在跨版本查询或原地升级入口。

Parent 修复 PR 使用同一数据库的 `run_repairs` 关系保存准确分支、PR、候选、默认分支和 Run 基线、
检查及实际合入事实。开放 PR 随原 Parent 修复更新，已合入记录不复用；
开发、审核、CI 调查额度仍归属原 Parent subject。协议升级不自动续接旧 Run，也不修改旧现场。

## 事实的唯一归属

[`storage.repository_schema`](../../src/agent_run/storage/repository_schema.py) 是唯一schema定义，`storage.sqlite.SCHEMA` 直接组合该定义：

| 事实 | 当前关系 |
| --- | --- |
| Task和仓库身份 | `repository_binding`、`tasks`、`task_runs` |
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
域内关系以准确Run复合键关联，物理输出以 `(run_id, invocation, ordinal)` 标识；原始返回、进程身份与活动事实属于准确输出，
不从后来的当前对象重建早先调用或审核依据。Attempt 的 `thread_id` 和 `opening_id`
由 continuation 入口绑定，分别引用 Thread 和不可变 Opening 材料。

凭据准备只在现有 `waits(kind=agent_preparation)` 预留准确 Attempt 的工作席位，
固定绝对截止最多 600 秒；读取、重试和兄弟进度不续期。准备席位与真实 lease
共同占用并发容量。凭据就绪后，同一事务以原 reservation ID 创建 Invocation/output/lease
并删除准备 Wait；执行期限从该准入点开始，Output Repair 仍继承原 Invocation 截止。
准备失败不创建 Invocation、不消耗 Development 用量；同一 Attempt 在最终 Worker
放行准入或实际返回工作证据处仅记账一次。准备取消只有准确资源清理完成或原 Host
退出证明才能释放席位；Future 完成及未知 Host 均不是清理证明。

旧协议的分配时扣费及未收口用量不重新解释。当前协议保留已审的准备→执行单一生命周期语义，
读取、写前及锁内均拒绝旧协议；不迁移、不补偿退款、不改写旧用量。保留原数据及匹配
Runner Snapshot，以原版本恢复；安装新 Runner 不意味着能够继续旧 Run。

需求/图版本、候选、预期合并树与 Ticket 完成事实保留准确关联，
后来的当前对象不得改写早先审核或批准依据。
明确指定但无法解析的需求/图 revision 必须拒绝，不能降为空引用。
批准、关闭 intent/receipt 及结果未知时的读回边界继续保留。

各角色的原始返回落盘与阶段消费是不同事务边界；重开后优先复用已存结果。
结果消费和适用预算变化由 Core 在同一业务事务收口，重复返回不能再次执行或记账。
原始返回尚未落盘的中断仍属于未知结果，不能假定成功。

`RunTransaction.put/get` 按准确Run、拥有者、用途、SHA-256和实际字节数保存及核验材料。
裸 `Transaction` 只用于显式仓库元数据，不隐含Run选择；各Run view共享原owner的累计读预算和poison。
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

SQLite 关闭最后连接时可能正在删除 WAL/SHM。只读准入若观察到原普通 sidecar
链接数为零，只在原读取预算及绝对截止内让步等待，每次至多 1 ms，并重新查询路径；
只有实际 `ENOENT` 才结束等待，持续零链接耗尽原预算仍拒绝。
不同 inode、非普通对象、非零链接及其他读取错误立即拒绝；等待后的主库、父目录
身份和真实 SQLite 快照核验保持，不能借此接管替代文件或获得新预算。

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

业务保存通过同一主SQLite的 `save_admissions` 排序。只承诺**有效登记提交后的顺序**，
不承诺调用先后或登记前公平；队列只保存不可变UUID、准确Run、clock域、截止与序号，
不保存业务正文、闭包或执行权。只读查询不登记、不清票。Core工作/集成FIFO职责不变。
同库不同Storage实例共用这一关系；进程内仅保留重入/after-fork所有权检查，没有第二deque。

原资格等待、preflight、登记、轮询和取得writer锁共用原 **1秒**竞争预算，
业务体至多执行一次；最终进入体前仍检查原截止与取消，这不是事务体或Agent运行时限。
至少有一次登记COMMIT和一次业务COMMIT，另有只读轮询及必要清票；WAL长读、争锁和
额外提交的成本仍须实测，不能以单元耗时推导真实跨进程性能。
全库活动与遗留票合计最多128。控制/Host/撤权和明确Task元数据入口使用原
`ordered=False`旁路，仍受同一SQLite锁、身份、容量、poison及提交约束，不能抢占已入体事务。

登记COMMIT未知只核原UUID；票缺或不可读不等于未提交，不重新登记或重放业务。
取消清票仅用原剩余预算，拿不到writer保留主异常与遗留票；已确认业务COMMIT成功
不能被finally清理或截止改成失败。未知业务写继续沿原intent/receipt恢复。

clock来源要求可信原生Linux、本地WAL、同一物理DB同时单kernel及未伪造的proc/队列表。
每次采样固定 `/proc/thread-self/ns/time` 与 `time_for_children`，两者须相同；
offsets新开原生procfs有界文件，读取前后重复核验两路径与原pin。
支持的owner线程及signal回调在请求期间不得变更namespace；端点观察不证明敌对ABA不存在。
同boot/provider/monotonic offset可比较；可信不同boot的合法 `/2`票只能在原登记事务内
整体分类后清理。未知域、同boot混offset和未证明绑定的 `/1`票拒绝保留，不猜过期。
容器重启不等于kernel boot变化。不支持网络WAL、多kernel并写或VM快照回滚。
**真实namespace、多进程FIFO、重启恢复和成本仍未验收，最终部署/验收仍阻断。**

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

## 集成席位与纯冲突

`runs.integration_holder` 与既有持久队列是唯一席位权威。
本地 `integrate` 失败中，只有 adapter 确认本次目标的 `MERGE_HEAD` 和真实未合并索引，
在原回执给出 `raw.merge_conflict=true` 时，当前 Ticket 保留席位完成有界冲突处理；
执行返回与丢回复后的原意图读回使用相同物理标准，不从错误文字猜测。
其他 Git 失败、CI 代码修复及 Review Findings 不因此保席。
冲突 Development 仍使用现有累计集成额度；不恢复免费调用，也不刷新原 Invocation 截止。

无法继续、人工阻塞或异常暂停经既有释放函数让位，显式 Stop/Abandon 保留原控制边界；
其中未结算的正式写入仍由原 pending-effect 检查阻止释放，须先按原意图读回。
恢复后按原队列规则重新竞争，不能恢复旧优先级；预算、Thread、Attempt 与资源义务分别保留。
