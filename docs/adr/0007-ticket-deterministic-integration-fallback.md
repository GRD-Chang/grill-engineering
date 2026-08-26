---
status: accepted
---

# Ticket Review 预算耗尽后按仓库 Required Checks 策略确定性集成

Ticket 不是最终产品交付边界，完整 Parent 的自动完成权威属于 Run Acceptance。为在信任 Agent 的 thin harness 中限制重复 Reviewer Token，每组配对的 Ticket Development 与 Review Budget Window 最多执行四次普通 Development Attempt 与三次 Reviewer Invocation：Development 1 后 Review 1，Development 2 后 Review 2，Development 3 后 Review 3；第三次仍失败且仍有普通 Development 名额时允许最后一次 Development 4。Finding、Git Integrity 与可修复 Required Check 失败复用这两项统一预算；每个新 Candidate 只要还有 Reviewer 名额就必须审查，三次名额用尽后才跳过。如果这些失败已提前耗尽四次 Development，Reviewer 失败后的 Candidate 不得未经修复进入兜底，而是等待人工 `resume`。唯一 Development 预算例外是四次普通 Development 耗尽后任一已发布 Ticket PR 首次出现经 CI Evidence 分类确认的可修复 Required Checks 失败：允许同一 Development Thread 额外执行一次 Final CI-fix；新 Candidate 仍有 Reviewer 名额时使用下一次 Fresh Acceptance，没有名额时才直接重新发布，准确新 head 再次出现可修复 CI 失败则等待人工 `resume`。

Review 预算耗尽后的 Candidate 不运行 Controller 本地代码 Validation、changed-surface 命令或 `git diff --check` 代码门禁。Controller 只执行 Git Integrity Check，并以绑定准确 base/tree、已用 Reviewer Artifacts、最后一次 Reviewer Artifact 之后的 Development Summary 和代码 delta 的 Fallback Publication Receipt 复用现有 Publication 阶段创建或更新普通 Ticket PR；Controller 不判断 Findings 是否关闭。它不增加 provisional PR 类型，也不把该凭据伪装成 Acceptance Record。Controller 随后读取准确 PR head 的实际 Required Checks：存在时必须全部通过，未配置时明确记录 `not_configured` 并允许继续；两种结果都形成不冒充 Acceptance Record 的 Deterministic Integration Record，再由 Published-Head Gate 决定是否将 Ticket 合入临时 Run Branch。最终 Run Acceptance 仍检查完整 Parent、跨 Ticket 交互及所有确定性兜底记录。

Git Integrity Check 或可修复 Required Check 失败都将原始证据返回同一 Development Thread；系统不增加 validation-fix、Git-fix 或独立 CI-fix 阶段。Ticket 复用 Run Repair 已有的 CI Evidence 分类，只有准确绑定当前 PR head、已完成且由配置的 code-failure step 证明可归因于代码的失败才启动 Development；pending、未知、证据不完整或矛盾、未配置 step、cancelled、runner 与暂时平台错误只进入有界 Controller 监督，监督到期后暂停等待 `run` 或 `resume` 继续读取，不消耗 Agent 或预算。Final CI-fix 只是一个条件式额外 Development Attempt，以 `attempt_kind=final_ci_fix` 和 `final_ci_fix_used` 记录；它不增加 Reviewer 额度，但会使用尚未消耗的 Reviewer Invocation。失败检查本身不计预算，只有实际 Development Attempt 计数。Development 无法解决时可返回 Human Blocker，普通预算与 Final CI-fix 均耗尽后等待人工 `resume` 开启新窗口。若 Resume 前仍保留需修改代码的准确失败证据，新窗口先由原 Development Thread 修复，产出新 Candidate 后才启动 Reviewer 1；Final CI-fix 后准确新 head 再次出现可修复 CI 失败时亦如此，不审查未修改的失败 Candidate。Agent 始终无权 commit、push、force-push、rebase 或 merge。

Ticket、Run 与 Parent-only 的每次 Reviewer 都按 Prompt 调用 `code-review` skill。每个窗口的 Reviewer 1 建立当前基线；Ticket Reviewer 2–3 以及 Run/Parent-only Reviewer 2–5 只额外收到紧邻上一轮 Acceptance Artifact 的完整原始内容及其角色化 review identity，Prompt 建议优先参考上轮问题和当前修复，但 Reviewer 自主决定审查顺序、范围以及是否全量审核。Ticket/Parent-only 使用 reviewed base/Candidate，普通 Run 使用 default base、Run head 与 expected merge tree，Run Repair Candidate 使用 Run base、Repair Candidate 与 expected merge tree。Controller 不解析 Findings、不维护 closure 状态，也不要求 Development 逐项报告处理结果。该策略不复用旧 pass，不改变 Run/Parent-only 的五次 Review、CI Evidence、Repair Cycle、人工批准或最终完成权威。

## Considered Options

- 最后一次 Repair 后再执行 Closure Review：保留最强 Ticket 语义证明，但继续消费 Reviewer Token。
- Review 预算耗尽后立即请求人工：安全但降低自动推进能力。
- 直接把确定性结果记为 Fresh Acceptance pass：会伪造 Reviewer 权威，因此拒绝。
- 在 PR 前运行本地 changed-surface validation：会形成第二套代码门禁、配置和修复路径，因此拒绝。
- 缺少 Required Checks 时暂停并要求人工配置或 `resume`：增加人工介入且把仓库未配置 CI 误当作运行故障，因此拒绝。

## Consequences

兜底路径在 PR 前形成 Fallback Publication Receipt，在准确 PR head 完成 Required Checks 策略求值后形成独立 Deterministic Integration Record。后者保留已使用 Reviewer Invocations 的状态、Artifact 与 reviewed Candidate、最后一次 Reviewer Artifact、随后 Development Summary 与代码 delta、Final CI-fix 使用情况及适用时的原失败 head/evidence 与修复 delta、准确 base/tree/head，以及该 head 唯一的 Required Checks Observation；Observation 的 `result=pass` 表示配置的必需检查全部通过，`result=none` 只表示仓库没有强制 CI，不得展示为 CI pass。新 Candidate、新 head 或 Required Checks 配置变化必须生成后继记录并使旧记录失效。两种记录都不得生成 Acceptance Record 或隐藏在普通 pass 状态中。Ticket 用尽三次 Reviewer Invocation 本身不产生 `review_budget_exhausted`；最后 Reviewer Artifact 已作为后续 Development Brief 输入并产生新 Candidate 时直接进入兜底，没有普通 Development 名额启动该后续 Development 时产生 `modification_budget_exhausted`。普通四次 Development 与条件式 Final CI-fix 都耗尽且仍需修改时等待人工 `resume`，恢复时开启新的 Ticket Review 与 Development 窗口、重置 Final CI-fix Allowance，并先把原始失败证据交给原 Development Thread；新 Candidate 产生后才进入新窗口 Reviewer 1。

删除本地代码 Validation 后，未配置 Required Checks 的兜底路径没有代码级测试证明；系统接受这一 thin-harness 取舍，但必须在状态、Deterministic Integration Record 与最终 Run Acceptance 输入中明确暴露 `not_configured`，不能伪装成 CI 通过。普通 Fresh Acceptance 路径仍沿用仓库实际 CI 策略。Ticket Completion 只表示已进入 Run Branch，不表示最终交付。Run Acceptance 必须读取并验证兜底证据，且仍是进入默认分支前不可跳过的完整语义验收；Run 自身每个 Review Budget Window 最多五次 Reviewer Invocation，第五次失败后只允许人工 `resume` 开启新窗口，不使用 Ticket 兜底。

本决策随 Review Budget Window、Final CI-fix 与兜底记录字段一起进行一次性规范状态切换，只适用于按新结构创建的 Delivery Run。既有旧 Run 不迁移、不兼容读取，也不根据旧 Attempt 历史推断新预算；Controller 在任何外部写操作前将旧结构拒绝为 `incompatible_run_state`，由维护者重新创建或清理该 Run。实现不增加 schema 版本、迁移器或新旧双轨状态机。
