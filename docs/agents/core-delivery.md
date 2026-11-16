# Core 交付规则的维护入口

三种交付仍由 `Kernel.next_work/accept_work` 的唯一选工路径推进，
共用原事务、预算、集成席位和逐写授权。内部模块不创建第二个控制器，
不接收完整 Kernel，也不接收其私有方法组成的回调集合。

## Parent 修复

`core/repair.py` 拥有修复候选、源需求、依赖图、Run/default 基线及修复 PR 的规则。

1. `start` 从独立验收后的准确候选固定修复身份；Core 在原事务提供 Review 当前性。
2. `advance` 观察候选与基线，返回 `EffectPlan` 或 `ChecksPlan`。
   `effect_binding` 组装修复专属的分支、候选、PR 与 payload；Core 准备通用 Effect。
3. 普通发布和修复都通过 `delivery_checks.record/consume` 保存准确 PR/head 检查证据，
   失败进入同一 subject 的 CI 修复，不重开 Parent 预算。
   检查等待由 Core `_checks_wait` 负责；成功后普通发布继续人工批准，修复继续 Run Branch 合入。
4. Core 在正式写前先检查 Execution、停止状态、原意图与派发身份，
   再用最新 World 和 Review 事实调用 `repair.authority`。
   准备时的判断不能替代这次授权；候选、需求或基线漂移不授权新写。
5. 回执先按原 Effect 校验，`repair.receipt_subject/consume` 解释原修复候选、
   保留修复历史并决定是否提升已验收成果。响应丢失时先读回原意图，
   PR 仍开放不足以证明合入未发生。当前规则变化也不抹掉已发送写入的实际结果。

`DeliveryState` 只保存当前操作时间，实际实现共同的门禁、历史事件、
集成快照、边界失效和源写记账。调用者传入原 Transaction；它没有 Storage、
Kernel、选工或真实写入权限。未结算 Effect 仍保护集成席位。
修复返回的计划不是授权，Core 仍拥有原事务与写入协议。

## Attempt 与读取失败

Attempt 的原始返回、退出、消费和恢复规则见 [Attempt 生命周期](attempt-lifecycle.md)。
`attempt_continuation` 读取原事实，`attempt_lifecycle` 判断等待与有界恢复，
Core 负责业务当前性、预算和结果消费。未知退出不提前释放租约或重新派发。

`OperationRunner.authorize_agent` 的读取失败使用原 `RequirementsRead.fence` 和 Execution；
`_prepare_authority` 的读取失败使用原 `EffectIO.token` 和 Execution。
两路都通过正式 `Kernel.wait_for_read` 保存失败，保留原 scope、started_at、deadline 和超时结果，
读取途中失效的身份不能把失败归给新工作。调用者不得重新选工、生成新 token 或延长窗口。
Abandon、Requeue、Cleanup 共用这个等待机制，各自保留控制动作和资源范围；
Abandon 必须先结算原已发送写入，不能把终止权限当作新写权限。

## Review 与边界检查

查询与 Core 只共用 `contracts.review_evidence.same_review_candidate` 的同义字段比较。
查询展示已记录事实，正式合入另外核验当前 World、Checks 和批准；
具体字段责任见 [展示事实](display-facts.md)。历史 Artifact 和结论保留。

`make typecheck` 先执行 `.github/ci/core_boundaries.py` 再运行严格 mypy。
前者检查上述能力不能导入完整 Kernel/Runner，覆盖函数体内导入、别名和相对导入；
后者检查实际调用方使用明确的 `DeliveryState`、Transaction、Attempt 等输入，
不能用完整控制器替代这些参数或通过转发访问不存在的控制能力。
结构检查不能证明事务或未知写入的语义，仍需正常 Core 入口的故障回归。

本边界沿用 [ADR 0032](../adr/0032-organize-core-by-change.md)，
没有引入通用工作流或 EffectJournal。
