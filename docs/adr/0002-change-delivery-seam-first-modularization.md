---
status: accepted
---

# Change Delivery 先收敛契约，再按领域分包

Development–Acceptance Engine 已被 Ticket Job、Parent-only Delivery 与 Run Repair Job 共同使用，但当前 `ChangeJobContract` 以十八个回调同时暴露固定事实、Agent 输入、Revision 判断、状态修改和持久化。我们决定保留单一 Engine，并以不可变的 typed descriptor 表达确定性事实，以一个语义 Adapter 表达三种任务真正不同的请求构造、PR 创建、过期恢复与收尾行为；共同的状态流转、Acceptance currentness、Required Checks、Published-Head Gate 和 merge 规则继续只由 Engine 实现。

Adapter 不直接执行 Git 或 GitHub 写命令，也不能任意修改完整 Run 状态；所有外部写入继续通过 Publisher，持久化继续通过 StateStore，并保留原子写与中断恢复能力。development、publication 与 review 使用三个类型明确的请求方法，确保 PR、处理 stale、合并收口与人工升级也保留独立的领域语义。项目仍处于 MVP 阶段，因此重构可以定义新的规范状态结构，不为历史 Run 状态提供迁移、兼容层或并行实现；代码使用职责名称，不用 `V1`、`V2` 一类后缀区分新旧设计。破坏性范围只覆盖 Change Delivery 的契约、Change Job 状态和 Acceptance Record，不借机重写 Controller、Run Publication、StateStore 或无关的 Run 状态。迁移先补三种任务的共同 characterization tests，再在原文件收敛 seam、删除旧回调集合，最后才建立 `agent_run/change_delivery/` 子包并逐步移动实现。

我们不采用按技术层一次性分包、第三方插件 hooks、通用 command bus 或全量状态模型迁移，因为当前只有三个同仓库、成套变化的 consumer，这些方案会增加接口、非法组合和恢复风险，而不能提高规则修改的局部性。详细成熟项目与工程标准证据见 [Python 单进程编排项目的渐进模块化与 Change Delivery seam 基准调研](../../research/2026-08-08-python-modular-architecture-benchmark.md)。
