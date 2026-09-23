# 已发布源分支以追加提交更新原 PR

<!-- status: accepted -->

Ticket、Parent-only 和 Run Repair 共用 Change Delivery 的 Candidate、验收、发布和恢复阶段。首次 Publication Commit 使用原准确 base；已有源分支发布 head 时，新 Publication Commit 保持 Candidate tree，并保留该 head 的祖先关系。目标 base 与旧发布 head 分叉时，Publication Commit 同时记录两者为父。冲突 Run Repair 的 publication 还保留默认分支祖先。Publisher 以普通 Git 快进推送更新原 PR，并在写入前后核对远端 head；意外推进或历史改写均保留本地成果、记录观测并停止。Closed、merged 或身份不匹配的 PR 不由本流程创建替代品。

发布文案由 Agent 在适用 Candidate 验收后生成；同一语义尝试的结果落盘后复用。新 Publication Commit 绑定新的 Required Checks 观测，不复制旧 SHA 的成功；响应丢失时按准确 PR/head 和持久写入意图对账。Run Repair 提升使用记录边界、必要祖先、准确 tree 与实际合入结果，而非假定每次 publication 都由原 base 直接派生。

本决策只覆盖没有新增远端漂移的更新。外部协作者的新提交在此阶段安全阻止；自动集成留给后续工作。目标分支已有 Ticket squash 与 Parent merge 策略保持不变。
