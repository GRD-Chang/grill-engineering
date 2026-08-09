---
status: accepted
---

# Change Delivery 先收敛契约，再按领域分包

Development–Acceptance Engine 已被 Ticket Job、Parent-only Delivery 与 Run Repair Job 共同使用，但当前 `ChangeJobContract` 以十八个回调同时暴露固定事实、Agent 输入、Revision 判断、状态修改和持久化。我们决定保留单一 Engine，并以不可变的 typed descriptor 表达确定性事实，以一个语义 Adapter 表达三种任务真正不同的请求构造、PR 创建、过期恢复与收尾行为；共同的状态流转、Acceptance currentness、Required Checks、Published-Head Gate 和 merge 规则继续只由 Engine 实现。Controller 在 Job 启动或恢复时从规范状态确定性构造 Descriptor 并直接注入 Engine，Adapter 不提供动态 descriptor factory。Parent-only 的显式批准只向 Engine 提交一次合并授权，批准后的 currentness、检查、普通 merge 与中断恢复不在外层 Controller 复制。

Adapter 不直接执行 Git 或 GitHub 写命令，也不能任意修改完整 Run 状态；所有外部写入继续通过 Publisher，持久化继续通过 StateStore，并保留原子写与中断恢复能力。Engine 创建并本地持久化唯一规范的 Acceptance Record；GitHub 只保留一条原地更新的简洁 Agent Run Status，不发布完整验收 JSON，旧 `record_acceptance()` Publisher 能力随重构删除。Run Acceptance 与 Run Repair 的 Revision Snapshot 使用按 Ticket number 数值排序的 typed Ticket Completion Revision，不嵌套复制完整 Acceptance Record。development、publication 与 review 使用三个类型明确的请求方法，确保 PR、处理 stale、合并收口与人工升级也保留独立的领域语义。项目仍处于 MVP 阶段，因此重构可以定义新的规范状态结构，不为历史 Run 状态提供迁移、兼容层或并行实现；Controller 在任何外部 mutation 前验证唯一规范 shape，不符合时返回 `incompatible_run_state`，不引入 schema 版本号。代码使用职责名称，不用 `V1`、`V2` 一类后缀区分新旧设计。破坏性范围只覆盖 Change Delivery 的契约、Change Job 状态和 Acceptance Record，不借机重写 Controller、Run Publication、StateStore 或无关的 Run 状态。迁移先补三种任务的完整 characterization matrix，再在唯一现有 Engine 内建立规范状态、typed views、持久化依赖和 Revision 基础，随后用一个原子 cutover 同时切换三个 consumer 并删除旧回调集合，最后才建立只承载共享 Engine、Protocol 与模型的 `agent_run/change_delivery/` 子包；三个 concrete Adapter 本次继续留在各自 consumer 附近。

我们不采用按技术层一次性分包、第三方插件 hooks、通用 command bus 或全量状态模型迁移，因为当前只有三个同仓库、成套变化的 consumer，这些方案会增加接口、非法组合和恢复风险，而不能提高规则修改的局部性。详细成熟项目与工程标准证据见 [Python 单进程编排项目的渐进模块化与 Change Delivery seam 基准调研](../../research/2026-08-08-python-modular-architecture-benchmark.md)。
