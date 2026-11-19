# 产品词汇索引

按当前任务直达专题。跨专题共用的交付对象、角色和证据见[核心词汇](GLOSSARY.md)。涉及 Core 实现中的 subject、attempt、effect 或 gate 时，查[Kernel 词汇](docs/architecture/GLOSSARY.md)。

| 当前任务 | 入口 |
| --- | --- |
| 安装、升级、回滚、宿主执行就绪 | [Runner 安装与管理](docs/domain/runner-setup.md) |
| 适配 Executor Host、前台或后台、动作准入、停止 | [执行宿主与生命周期](docs/domain/execution-control.md) |
| Worker 隔离、GitHub 身份、Agent 调用或个人方法 | [Worker、调用与身份](docs/domain/worker.md)及[Prompt 合同](docs/agents/agent-prompts.md) |
| 选定仓库或 Run、需求版本、Ticket 依赖或状态归属 | [交付对象与需求](docs/domain/run-scope.md)及[状态存储](docs/agents/state-storage.md) |
| Reviewer、验收范围、Finding 或证据适用性 | [独立验收与证据](docs/domain/acceptance.md) |
| Ticket 并发和集成、候选发布、Required Checks 或正式写入 | [Ticket 交付与正式发布](docs/domain/ticket-delivery.md) |
| Parent-only、整体交付、Run Repair PR 或最终批准 | [Parent 整体交付](docs/domain/parent-delivery.md) |
| Resume、同一 Attempt 续接、预算耗尽或续期 | [恢复、续接与预算](docs/domain/recovery.md) |
| status/history、工作时长或通知 | [进度、历史与通知](docs/domain/views.md) |

实现责任见[当前架构](docs/architecture.md)，关键架构决定及其修订见 [ADR](docs/adr/)。跨专题共享的术语在核心词汇定义，详细行为由对应专题维护。
