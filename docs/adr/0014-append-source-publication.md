# 已发布源分支以追加提交更新原 PR

## 后续取代：完整 Parent 修复直接交付

[ADR 0025](0025-parent-repair-without-internal-pr.md) 取代本文中 Run Repair 使用内部 Publish、PR、CI 与 PR 合并生命周期的条款。完整 Parent 的修复仍共用 Agent 执行、预算、证据与准确写入核验能力，但直接由程序更新 Run Branch，仅最终 Run PR 承担文案、必要 CI 与人工批准。Ticket、Parent-only 及其余祖先关系、未知写入先对账与恢复合同保持有效；下文相关内部 PR 描述保留为历史决策。


<!-- status: accepted -->

Ticket、Parent-only 和 Run Repair 共用 Change Delivery 的 Candidate、验收、发布和恢复阶段。首次 Publication Commit 使用原准确 base；已有源分支发布 head 时，新 Publication Commit 保持 Candidate tree，并保留该 head 的祖先关系。目标 base 与旧发布 head 分叉时，Publication Commit 同时记录两者为父。冲突 Run Repair 的 publication 还保留默认分支祖先。Publisher 以普通 Git 快进推送更新原 PR，并在写入前后核对远端 head；意外推进或历史改写均保留本地成果、记录观测并停止。Closed、merged 或身份不匹配的 PR 不由本流程创建替代品。

发布文案由 Agent 在适用 Candidate 验收后生成；同一语义尝试的结果落盘后复用。新 Publication Commit 绑定新的 Required Checks 观测，不复制旧 SHA 的成功；响应丢失时按准确 PR/head 和持久写入意图对账。Run Repair 提升使用记录边界、必要祖先、准确 tree 与实际合入结果，而非假定每次 publication 都由原 base 直接派生。

本决策只覆盖没有新增远端漂移的更新。Ticket 对外部协作者追加的后续集成见 [ADR 0015](0015-ticket-remote-integration.md)；目标分支已有 Ticket squash 与 Parent merge 策略保持不变。
