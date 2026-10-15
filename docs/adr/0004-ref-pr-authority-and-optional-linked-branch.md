# ADR 0004：远端 Ref 与 PR 是分支交付的权威，Linked Branch 仅作展示

## 后续取代：完整 Parent 修复直接交付

[ADR 0025](0025-parent-repair-without-internal-pr.md) 取代本文中 Run Repair 使用内部 Publish、PR、CI 与 PR 合并生命周期的条款。完整 Parent 的修复仍共用 Agent 执行、预算、证据与准确写入核验能力，但直接由程序更新 Run Branch，仅最终 Run PR 承担文案、必要 CI 与人工批准。Ticket、Parent-only 及其余祖先关系、未知写入先对账与恢复合同保持有效；下文相关内部 PR 描述保留为历史决策。


## 状态

Accepted

## 决策

Delivery Run 以确定性分支名、远端 `refs/heads/<branch>` 的准确 SHA、CAS push 回读，以及 PR 的 head/base 身份创建、恢复和校验；不再使用 `gh issue develop` 或 GitHub Linked Branch 元数据判断分支是否存在或是否可恢复。Linked Branch 只在首次分支创建时通过无本地 Git 配置副作用的 API 尝试建立，用于 Issue 页面展示；空返回、读回缺失或调用失败只记录为不可用，不阻塞、不重试，也不在后续 `run`/`resume` 中补挂。已有旧状态不做迁移或兼容。

## 后果

Publisher 必须将 Parent、Ticket 与 Run Repair 的远端 ref 查询、SHA 校验、CAS 创建/发布、PR 查询与回读收敛为同一机制。维护者只通过 `run` 进入自动生命周期，内部 delivery、acceptance 与 publication 阶段不再构成手工串接的公开流程。最终批准只授权准确的 PR head/base 与验收结果；同一事实集合的 GitHub 收敛可自动监督，任何新 head 或 base 必须重新验收和批准。
