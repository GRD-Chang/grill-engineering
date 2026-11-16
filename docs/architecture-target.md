# 目标架构决策

状态：方向已采用，分阶段实施。分析基底为
`ed90d14ceb887e54e3a3a4d05374a45db32da499`；本页描述目标，不表示已经完成。
现行实现见[当前运行架构](architecture.md)，业务术语和角色合同见[产品上下文](../CONTEXT.md)。

Core 交付规则与恢复的已实施维护入口见 [Core 交付合同](agents/core-delivery.md)：
修复能力、共同 Checks、Attempt 和 Review 同义字段比较各有明确归属，
共同选工、原事务和逐写授权仍由单一 Core 负责。

## 决策与职责

保留一个 Core，明确分离当前业务状态、物理执行证据、已提交业务事件。
Ticket、Parent-only、Final 共用唯一 selector、事务边界和授权规则。
模块化的目的是缩小接口、退出重复解释机制；文件拆分本身不计代码减量。

| 模块边界 | 职责与依赖 |
| --- | --- |
| Core 门面与 OperationRunner | Core 选择工作并生成完整读取／操作请求；Runner 只协调有界 I/O 和结果交还，无 SQL，不组装业务上下文，不另选后继 |
| Core 内部业务服务 | Delivery、Agent、Effect、Control 各有内聚职责，共享调用者事务；不各自建立调度器或嵌套业务事务 |
| Storage / Task | SQLite 事实、不可变材料、事务、容量与 Task 绑定；依赖底层值类型，不决定业务后继 |
| Runtime / adapters | Host、Worker 监督和真实 Git/GitHub/工作区操作；依赖明确端口合同，不读取 Core SQL，不制定预算或授权 |
| Queries / notifications | 共用只读事实投影；查询只展示，通知另有投递账本和 transport；两者不反向调用 CLI 或彼此推导业务状态 |
| CLI / configuration / installation | 组合具体实现、解析输入、冻结配置及管理安装；底层值类型和错误码不依赖 CLI、本地化或个人默认存储 |

依赖方向为入口 → Core／Runtime／查询／通知，Core → 内部业务服务 → Storage，
物理实现 → 端口合同，查询与通知 → 只读事实投影 → Storage。
具体实现由组合入口注入；Core 不导入具体 adapter，adapter 通过注入的授权接口回调。
可据此组织 `core/`、`storage/`、`runtime/`、`adapters/`、`queries/`、
`notifications/`、`cli/` 等包；目录调整与真实机制替换分别核算。

## 内部合同

| 合同 | 含义 |
| --- | --- |
| WorkIdentity | 原 Action、Effect、Attempt，或明确的交付阶段身份；不是当前 Host 身份 |
| ReadFence | 本次读取涉及的 epoch、Source 序列、控制及对象适用性；仅在本次 I/O 中使用 |
| WaitWindow | 按业务身份保存原开始时间、绝对 deadline 和 backoff；重复读取、审计或换 Host 不续期 |
| AdvanceResult | 明确区分等待、AgentCall、物理操作、观测与真实业务变化；诊断写入不算业务进展 |
| Intent / Dispatch / Observation | 分别保存不可变原请求、准确派发及实际写响应、原始物理观察；unknown 保持未知 |
| CommittedEvent | 同一业务事务内记录的明确变化、原对象身份、因果 Action 和证据引用；不承担当前业务状态权威 |

类型和证据本身不是授权凭据。每次真实写入仍需核对持久身份、当前权限及物理边界；
sent intent 的只读结算不授予新写权限。Agent 原始返回、物理退出和业务消费分别处理，
保留 Action／Attempt／Invocation／Opening／Thread、预算、租约和输出归属。
未知退出不能释放租约，Future 完成不能代替退出证明。

事务内选择或提交，事务外执行物理 I/O，返回后复核原身份、epoch、适用性和截止。
晚到的真实证据可以入审计，不能据此恢复已失效的授权。
`state` 和 `run_once` 使用同一 choice；纯 status/history 不推进或激活 Runner。
正式 Host 的有限初始观察按原身份公平轮转，ReadWait 或 CAS 失效不消费成员，重复激活不重置集合。
单次操作有界，分配 AgentCall 后立即交还 Host。

## 替代顺序

**A：显式观察／工作合同与包边界。** Core 生成完整请求并接受事实，移除 Runner SQL、
深私有耦合及调用者拼装业务上下文的入口。用明确身份区分读取 fence 和持久等待窗口，
退出不透明 JSON token／序列化 scope 的组合协议。
无进展保护只有在真实业务变化能由事务内受条件保护的转换证明后才能替换；
不得用新增历史行、全局计数器或裸 rowcount 冒充进展。具体接口名可随实现细化。

**B：单一 EffectJournal。** 集中原 intent 绑定、派发计数、实际响应、观察及结算协议，
退出普通推进、Requeue、Abandon、cleanup 对共同生命周期的重复处理。
各业务 handler 保留自己的顺序、适用性、补偿和重试权限；不通用化成工作流解释器。
输入校验、物理归属证明、当前写授权仍是不同信任阶段。
prior 必须重新获得当前写权限；prepared 且派发次数为零不能凭 target 冒认执行。
真实非零写响应与成功写后读失败分别审计，不合成返回码，不丢原 intent 或部分资源证据。
业务完成与资源收尾分别表达；Abandon 后仍保留显式 cleanup 义务。

**C：提交时明确业务因果。** 在实际变更事务中记录门禁进入／退出、Attempt 分配、
结果消费、Effect 结算、Delivery 完成和控制进展，并关联原身份及本次发生身份。
Action 接受、Host 启动、物理返回和业务完成分别记录；Resume 不关联到后来碰巧发生的事件。
查询与通知消费明确事实，退出门禁快照重放、历史区间猜因果和重复关联认证。
独立通知账本继续负责发送次数、截止、unknown 和故障隔离，通知故障不阻断业务。

管道泵独立推进，保留严格输出、尾部诊断、在线 guard、进程身份及各调用者的退出策略。
各切面先固定交界合同，完成后退出旧实现，不保留长期双路；共享 schema／入口集中装配。

## 实施顺序补注

C 的门禁 CommittedEvent 暂缓：旧重放约55行，替代需要多表发生身份和多个写点，
并影响 `history.status_snapshot`；尚未证明保留功能且整体更简单，schema 保持不变。
B 的 EffectJournal 尚无独立收益证明，暂不自动启动。
A2 的重复 Work 壳／字符串注册方案净增110行，已退回重做。
原设计推导保留；以上是当前取舍，不表示相关替代已经完成。

## 范围与关键节点

不引入第二 selector、缓存 World、Receipt 握手、全量事件溯源、通用 CommandBus、
插件框架或多继承 mixin。保留 FIFO／capacity、集成单席、准确审核与批准、Stop／Resume／Abandon、
逐写授权、CAS、dirty／foreign 保护及恢复合同。
不兼容旧协议，不隐式迁移、覆盖或删除旧数据；旧 Task 占用继续拒绝不安全重建。

架构节点只做相关的小合同检查：A 检查正式 Host 多 intent 公平观察、慢读越原截止与被动查询；
B 检查真实写后未知读回、prior 重新授权与 foreign 保护；C 检查门禁重入、准确 Resume 因果、
通知故障隔离及只读查询。同时检查单 selector、Runner 零 SQL 和单向依赖。
完整功能验收安排在架构稳定后，节点通过不借用其他提交的测试结果。

基底全部生产 Python 为 87 文件、22,621 行，含 adapters、空行和注释。
A／B／C 的静态净减设计估计分别为 0–100／0–250／150–300 行，合计约 150–650 行；
这是比较替代方案的假设区间，不是实现下界、交付承诺或从 1 万行目标倒分配的预算。
已分配的管道泵和只读分类收益不重复计入，新增类型／存储代码计入净差，移动目录不算删除。
1 万行仍是目标，当前没有足够证据承诺达到；每个结构节点按相同口径重新测量。

设计参考：[SQLite 事务](https://www.sqlite.org/lang_transaction.html)的事务边界，及
[事务 outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
中业务变化与待消费事实同事务提交的原则；不据此引入外部工作流服务或改变投递保障。
