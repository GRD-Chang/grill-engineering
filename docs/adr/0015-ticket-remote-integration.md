# Ticket 在固定审查基线后集成远端追加

<!-- status: accepted -->

Ticket Development 与 Fresh Acceptance 绑定创建 Job 时的 base 和 Candidate。源分支或 Run Branch 的普通追加不重启开发，也不使已有语义审查轮次失效。取得原 Candidate 的 Acceptance Record 或 Fallback Publication Receipt 后，共享 Change Delivery Engine 读取源与目标的 live SHA；Git 以准确提交对象机械集成双方成果，Publisher 再根据集成后的 tree 创建或追加 Publication Commit。初始源 ref 若等于原 base，只代表分支起点。原 Ticket 审查对象与集成结果分别记录，后者不得冒充新的 Ticket Acceptance。

首次集成不计次数。完成一次集成后，仅外部源或目标的新快照消耗 `integration_resync_limit`；内置上限为三次，绑定 Run 有效策略。自己的发布与回读、同一快照的重试不计数。额度耗尽时保留 Job、checkout、PR 与历史并暂停，只有显式 `resume` 续期该额度；普通暂停不会因远端追加自行恢复。旧 publication 与其 Required Checks 观察在外部快照变化时失效。实际合入前再次读取 live refs 并核对集成记录，合并仍绑定准确 PR head；服务端最后瞬时竞争由远端准确 head 检查处理。

真实合并冲突保留 Git 现场，交由同一 Development Agent 解决并自测；这项纯冲突工作不消耗需求开发、Reviewer 或 CI 修复额度，也不新增 Fresh Acceptance。失败或业务歧义有界暂停。网络或执行错误不归类为 Git 冲突；非追加历史改写明确暂停且不覆盖远端成果。受管 checkout 的 Git 元数据保护继续生效。Parent Acceptance 可读取原审查事实与真实集成记录，审查完整累计结果。

状态协议随新增持久字段切换；旧 Run 在外部写入前按现有 MVP 规则拒绝，不推断缺失额度，也不隐式迁移。实现沿用共享 Engine、Publisher、StateStore 和有限的 Ticket Adapter；Git 集成动作可供后续 Parent 与 CI 路径调用，但本决策不改变它们的业务流程。
