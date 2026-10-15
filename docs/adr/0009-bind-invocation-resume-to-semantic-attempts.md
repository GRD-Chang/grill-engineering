---
status: accepted
---

# Invocation Resume 绑定 Semantic Agent Attempt，不按进程重复计数

后续修订：[ADR 0011](0011-resume-interrupted-output-step.md) 增加每个 Semantic Agent Attempt 对普通异常最多一次、对准确容量错误持续进行的 Worker 自动恢复，并要求 JSON 修复中断后的默认 Resume 续接原步骤。下文“不自动重试”与格式额度规则按该窄例外解释；本 ADR 的语义计费规则继续适用。

Development、Reviewer 与 Publication 统一以 Semantic Agent Attempt 表达一轮语义工作，不再以 Codex 进程启动次数表达领域 Attempt。每个 Attempt 在首次调用前绑定 role、Work Subject、Job Generation、Currentness Boundary fingerprint 与 role-local ordinal；Development 和 Reviewer 还绑定适用的 Budget Window，Publication 不伪造不存在的业务预算窗口。初始 Agent Invocation、Output Repair、进程失败后的 successor Invocation、Human Blocker Resume 与显式 `--new-thread` 都必须引用同一 Attempt identity；只有当前 Attempt 形成完整角色结果，或其 Currentness Boundary 失效并按现有 stale 规则收口后，才能分配后继 Attempt。

Development 在 Attempt 分配时占用一次 Development Budget，因为 Worker 可能已修改 Managed Development Checkout；Reviewer 在形成合法 Acceptance Artifact 时占用一次 Review Budget；Publication 在分配时计入一次 Publication Attempt。三种角色的 Invocation Resume 都不重复计数。已持久且尚未收口的 pending Attempt 优先于新 Attempt 的预算门禁：即使窗口计数已达上限，Controller 也必须继续该 pending Attempt，不得将它拒绝为预算耗尽。只有不存在 pending Attempt 且真正进入 `modification_budget_exhausted` 或 `review_budget_exhausted` 时，维护者显式 Resume 才开启新的编号 Budget Window，并在该窗口中分配新 Attempt。显式 Resume 没有第二套硬上限；每次人工授权、失败原因与 successor Invocation 都进入 `status`/`history` 审计。本决策不增加自动进程重试：零退出但输出不符合 contract 时仍只在当前 Invocation 内最多执行两次 Output Repair，其他进程、timeout、signal、sandbox、凭据、缺少最终输出或 Thread mismatch 失败仍停在 `execution_failed` 等待显式 Resume。

Publication 的 Agent 语义生成与外部操作恢复分开计数。Publication Attempt 只跟踪 Publication Codex 对准确发布边界的一轮语义工作；GitHub 读取收敛、带精确绑定的写入与结果对账使用独立、有界的 Publication Operation Retry，不增加 Publication Attempt，也不被 Invocation Resume 清零或绕过。耗尽后的定向人工 Resume 按 ADR 0017 建立新的操作重试窗口，并保留已耗尽窗口的历史。

Managed Development Checkout 是尚未形成 Candidate 时的交付成果边界。维护者中断前台调用只表示可恢复暂停，不授权删除工作区；这一决策有意替代现有运行手册、实现与测试中“操作者显式取消即终态清理”的旧边界。普通 `run`、`resume` 和自动 cleanup 发现 tracked modifications 或 untracked files 时必须 fail closed，保留路径、原因与恢复动作。普通 `abandon` 必须在任何外部 mutation 前扫描全部 Managed Development Checkout 并在发现 dirty checkout 时拒绝；只有额外显式的 `--discard-worktree` 才授权不可恢复删除。Validation Checkout 等一次性验证工作区不属于这一交付成果边界。

## 统一暂停续接

恢复入口不以 reason 白名单作为永久准入规则。当前架构内的 Ticket、Parent-only、Run Repair、Run Acceptance 和 Final Publication 保留原目标、对象、角色、成果、证据及待完成阶段；人工处理后由同一 `resume` 复核事实并启动适用后继。暂停前阶段或准确 pending Attempt 决定续接，已有完成成果继续消费，不新开初始开发。未知原因的原始证据留在暂停历史与 Resume 审计；材料不足时明确缺口，不猜测外部写入结果。

`--message` 附加到当前工作 Generation，并进入对应角色的实际 Prompt；它不接受新需求、任务图或替代最终批准。普通 Resume 不续期额度，只有额度耗尽后的明确继续才追加对应窗口并保留旧窗口。定向 Ticket 恢复仍使用已有 FIFO、共享名额与锁，不解除其他 Ticket 的独立人工门禁。旧状态不自动迁移，也不删除成果。

当前架构的协议身份仍完整、但必要字段缺失或不一致时，公开入口返回 `recovery_materials_missing`，说明 Run、失败的字段合同和补足条件。诊断不写回残余 Run，不创建后继工作或把缺损永久记录为不可恢复；人工从可信备份恢复原事实后，同一 `resume` 重新执行完整状态及身份核验。协议身份不成立的旧 Run 仍不兼容，不补默认值、不迁移。

已开展工作的受管 checkout 丢失时，恢复准入在接受 Action 前报告当前工作对象、路径、保存的准确分支与提交及对象是否可取得，不静默创建空工作区来冒充原成果。人工须补建真实 Git worktree，并补足有备份的未提交成果及必要集成现场；提交对象不能证明丢失的未提交内容已经恢复。一次性验证工作区、已完成工作及其他 Ticket 的独立暂停不构成当前对象的恢复缺口。补足后仍由既有阶段重核需求、范围、基线、成果及适用的审核、文案、CI 和批准，不增加恢复专用放行规则。

恢复不重建丢失历史，不猜测验收、授权及外部副作用记录。外部操作结果未知继续先对账；本切片不增加副作用记录缺损重建。工作区补建及新会话仅承接原工作，不重置额度，也不吸收正式需求或 Ticket 图变化。

工作区补建沿用 [Git worktree 官方合同](https://git-scm.com/docs/git-worktree)：在已有准确分支上添加 linked worktree，提交对象与 worktree 的索引、未提交文件和合并现场是不同材料。程序不自动 reset、清除残余现场或把可取得提交当作全部材料；这里优先采用具体诊断和人工补足，随后复用既有门禁，而非增加第二套恢复状态机。

全局 Ticket 执行恢复核验本次可运行的整个 frontier，不只检查消息所属的第一个 Ticket；局部暂停及尚未解除的独立门禁仍排除在外。各交付 lane 在准备现场前再次检查。pending Development 在调用 Agent 前比对原受管仓库的公共 Git 元数据、准确分支及保存 HEAD（集成工作使用保存的现场 HEAD）；人工补建到已前进的同名分支会被拒绝，保留现场供人工校正，不进入 Agent 越权修改的 reset/clean 修复路线。

## Considered Options

- 只调整 Development 的 `pending_attempt` 门禁：能修复 Issue #140 的直接错误，但 Reviewer 的 validation 序号和 Publication 的混合计数仍会把 Resume 视为新 Attempt，因此拒绝。
- 为 Development、Reviewer 和 Publication 建立完整通用 Attempt 状态机：能够统一所有计数时点，但会复制并侵入现有 Change Job、Review Budget 与 Publication 控制流，因此只增加共享 Attempt identity，各角色保留自己的完成与计费规则。
- 对 Codex 异常退出自动重试：可以减少人工 Resume，但无法安全把操作者中断、持续性凭据或 sandbox 错误、失控 timeout 与瞬时故障统一归类，因此保留显式 Resume。
- 为旧 Run 增加兼容读取或状态迁移：会引入第二套 Attempt 解释，也可能将已矛盾的 Job/Invocation 事实自动改写为权威状态，因此本次执行一次性规范状态切换，不兼容、不迁移旧 Run。

## Consequences

Agent Invocation 必须持久化并校验它所属的 Semantic Agent Attempt；各角色必须在启动 Codex 前保存可恢复的 pending identity，在形成权威角色结果后才收口。新 Runner 对不含规范 Attempt identity 的旧 Run 和 Job、Attempt、Active Invocation 相互矛盾的状态都拒绝执行，停止 Codex、Publisher mutation 与自动 cleanup，且不提供迁移器。`status` 与 `history` 需要区分 Semantic Agent Attempt、Agent Invocation、Output Attempt 和 Publication Operation Retry。本 ADR 记录的是已接受但尚待实现的目标合同；在源码、公共 CLI 测试与运行手册原子切换前，`docs/agent-run.md` 仍描述当前 Runner 的实际行为。

该规则属于 Controller 私有流程；Development、Reviewer 和 Publication Prompt 仍只接收完成局部交付所需的任务事实。Reviewer 接收当前与剩余自动验收次数，定向 Repair 接收已完成与剩余次数；这些值从现有计数机械投影，不暴露 Budget Window、Resume 次数或后继状态机，也不改变验收标准。实现必须以 Development、Reviewer 和 Publication 的公共 CLI 恢复路径验证 Resume 不重复计数，并覆盖 Currentness 失效、真正预算检查点、Ctrl-C、dirty checkout、普通 `abandon` 与 `--discard-worktree` 的反向边界。

本 ADR 扩展 ADR 0003 对 Invocation Resume、Output Repair 与 Requeue 的分层：进程失败仍不自动重试，Output Repair 仍是同一 Invocation 内最多两次的有界格式修复，Requeue 仍只处理 stale Job Generation。ADR 0003 中“十次 Ticket Repair Budget Window”的旧数量已由 ADR 0007 的四次普通 Development 加一次条件式 Final CI-fix 决策替代，本 ADR 不恢复该旧数量。
