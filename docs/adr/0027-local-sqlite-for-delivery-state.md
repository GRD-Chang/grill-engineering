---
status: accepted
---

# 使用本地 SQLite 保存交付状态

2026-10-15 归档注记：本地 SQLite 已用于业务状态，现行实现合同见[状态存储](../agents/state-storage.md)；控制同库仍是 [ADR 0029](0029-unified-control-and-business-storage.md) 的后续目标。表示与资源限制的进一步取舍见 [ADR 0028](0028-bounded-business-object-persistence.md)。下文保留最初决策时的说明。

本决策已确认，尚未实施。新 Run 使用本地 SQLite 保存按交付对象组织的工作事实与记录，利用短事务共同提交相关变化；它实现 [ADR 0026](0026-object-owned-state-and-explicit-recovery.md) 的对象级提交方向。相比自行维护对象文件或快照日志的提交、重放与修复协议，选择 SQLite 是为了复用成熟的本地事务能力，并保留现有 StateStore、Task Control 和阶段职责。

用户明确要求控制复杂度：记录组织围绕当前业务对象，查询与 JSON 导出按需组装，保存以持久提交为准，控制新增模块、表、线程及存储副本。线程组织、数据库范围、Task Control 的共同事务边界和历史保留规则由 Agent 结合现有合同设计，不能由本决策推定已实现；恢复授权、新 Run 范围与既有发布权威继续遵守 ADR 0026。
