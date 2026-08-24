---
status: accepted
---

# Invocation Resume 绑定 Semantic Agent Attempt，不按进程重复计数

Development、Reviewer 与 Publication 统一以 Semantic Agent Attempt 表达一轮语义工作，不再以 Codex 进程启动次数表达领域 Attempt。每个 Attempt 在首次调用前绑定 role、Work Subject、Job Generation、Currentness Boundary fingerprint 与 role-local ordinal；Development 和 Reviewer 还绑定适用的 Budget Window，Publication 不伪造不存在的业务预算窗口。初始 Agent Invocation、Output Repair、进程失败后的 successor Invocation、Human Blocker Resume 与显式 `--new-thread` 都必须引用同一 Attempt identity；只有当前 Attempt 形成完整角色结果，或其 Currentness Boundary 失效并按现有 stale 规则收口后，才能分配后继 Attempt。

Development 在 Attempt 分配时占用一次 Development Budget，因为 Worker 可能已修改 Managed Development Checkout；Reviewer 在形成合法 Acceptance Artifact 时占用一次 Review Budget；Publication 在分配时计入一次 Publication Attempt。三种角色的 Invocation Resume 都不重复计数。已持久且尚未收口的 pending Attempt 优先于新 Attempt 的预算门禁：即使窗口计数已达上限，Controller 也必须继续该 pending Attempt，不得将它拒绝为预算耗尽。只有不存在 pending Attempt 且真正进入 `modification_budget_exhausted` 或 `review_budget_exhausted` 时，维护者显式 Resume 才开启新的编号 Budget Window，并在该窗口中分配新 Attempt。显式 Resume 没有第二套硬上限；每次人工授权、失败原因与 successor Invocation 都进入 `status`/`history` 审计。本决策不增加自动进程重试：零退出但输出不符合 contract 时仍只在当前 Invocation 内最多执行两次 Output Repair，其他进程、timeout、signal、sandbox、凭据、缺少最终输出或 Thread mismatch 失败仍停在 `execution_failed` 等待显式 Resume。

Publication 的 Agent 语义生成与外部操作恢复分开计数。Publication Attempt 只跟踪 Publication Codex 对准确发布边界的一轮语义工作；GitHub 读取收敛、带精确绑定的写入与结果对账使用独立、有界的 Publication Operation Retry，不增加 Publication Attempt，也不被 Resume 清零或绕过。

Managed Development Checkout 是尚未形成 Candidate 时的交付成果边界。维护者中断前台调用只表示可恢复暂停，不授权删除工作区；这一决策有意替代现有运行手册、实现与测试中“操作者显式取消即终态清理”的旧边界。普通 `run`、`resume` 和自动 cleanup 发现 tracked modifications 或 untracked files 时必须 fail closed，保留路径、原因与恢复动作。普通 `abandon` 必须在任何外部 mutation 前扫描全部 Managed Development Checkout 并在发现 dirty checkout 时拒绝；只有额外显式的 `--discard-worktree` 才授权不可恢复删除。Validation Checkout 等一次性验证工作区不属于这一交付成果边界。

## Considered Options

- 只调整 Development 的 `pending_attempt` 门禁：能修复 Issue #140 的直接错误，但 Reviewer 的 validation 序号和 Publication 的混合计数仍会把 Resume 视为新 Attempt，因此拒绝。
- 为 Development、Reviewer 和 Publication 建立完整通用 Attempt 状态机：能够统一所有计数时点，但会复制并侵入现有 Change Job、Review Budget 与 Publication 控制流，因此只增加共享 Attempt identity，各角色保留自己的完成与计费规则。
- 对 Codex 异常退出自动重试：可以减少人工 Resume，但无法安全把操作者中断、持续性凭据或 sandbox 错误、失控 timeout 与瞬时故障统一归类，因此保留显式 Resume。
- 为旧 Run 增加兼容读取或状态迁移：会引入第二套 Attempt 解释，也可能将已矛盾的 Job/Invocation 事实自动改写为权威状态，因此本次执行一次性规范状态切换，不兼容、不迁移旧 Run。

## Consequences

Agent Invocation 必须持久化并校验它所属的 Semantic Agent Attempt；各角色必须在启动 Codex 前保存可恢复的 pending identity，在形成权威角色结果后才收口。新 Runner 对不含规范 Attempt identity 的旧 Run 和 Job、Attempt、Active Invocation 相互矛盾的状态都拒绝执行，停止 Codex、Publisher mutation 与自动 cleanup，且不提供迁移器。`status` 与 `history` 需要区分 Semantic Agent Attempt、Agent Invocation、Output Attempt 和 Publication Operation Retry。本 ADR 记录的是已接受但尚待实现的目标合同；在源码、公共 CLI 测试与运行手册原子切换前，`docs/agent-run.md` 仍描述当前 Runner 的实际行为。

该规则属于 Controller 私有流程；Development、Reviewer 和 Publication Prompt 仍只接收完成局部交付所需的任务事实，不注入 Attempt 序号、剩余预算、Resume 次数或后继状态机。实现必须以 Development、Reviewer 和 Publication 的公共 CLI 恢复路径验证 Resume 不重复计数，并覆盖 Currentness 失效、真正预算检查点、Ctrl-C、dirty checkout、普通 `abandon` 与 `--discard-worktree` 的反向边界。

本 ADR 扩展 ADR 0003 对 Invocation Resume、Output Repair 与 Requeue 的分层：进程失败仍不自动重试，Output Repair 仍是同一 Invocation 内最多两次的有界格式修复，Requeue 仍只处理 stale Job Generation。ADR 0003 中“十次 Ticket Repair Budget Window”的旧数量已由 ADR 0007 的四次普通 Development 加一次条件式 Final CI-fix 决策替代，本 ADR 不恢复该旧数量。
