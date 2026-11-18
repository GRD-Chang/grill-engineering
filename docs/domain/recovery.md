# 恢复、续接与预算

操作员恢复意图、原 Attempt 续接及耗尽预算窗口的不同授权。物理调用的退出和收口见[调用生命周期](../agents/attempt-lifecycle.md)，宿主启动与停止见[执行宿主](execution-control.md)。

[按任务查阅术语与合同](../../GLOSSARY-MAP.md)

## 恢复意图与 Invocation 续接

**Invocation Resume（调用恢复）**:
调用层的续接动作，其业务含义必须区分 Business Resume 与 Execution Recovery；复用同一 Thread 或生成 successor Invocation 本身不表示授权了新的预算窗口。公开 `resume` 保持单一入口，本次恢复意图绑定准入时的准确暂停原因、对象与授权，不能因后续状态变化被重解释；自动执行续接不具有解除业务阻塞或开启预算窗口的权限。以下保留公开 `resume` 的承载关系，不能据此把两种语义合并。
维护者以 `agent-run resume <parent-issue>` 继续当前 `execution_failed` 或 Human Blocker 工作，需要 Agent 时创建 successor Invocation。若失败 Invocation 属于一次已分配且尚未收口的 Development、Reviewer 或 Publication Attempt，Resume 只继续该语义 Attempt，不再次占用角色预算或增加 Attempt 计数，也不得接受 Delivery Policy 覆盖；这一原则与是否复用原 Thread 无关。每次 Resume 都是新的显式人工授权，系统保留其次数与失败原因供 `status`/`history` 审计，但不为同一 Semantic Agent Attempt 另设 Resume Budget 或硬上限。后台模式的 Ctrl-C 或关闭发起终端只离开观察，后台 Host/Executor 继续推进；停止须使用公开 `stop`，不表示放弃 Attempt 或授权清理 Managed Development Checkout。
预算耗尽检查点的显式 Resume 按准确 subject、purpose 和 gate 续期对应窗口，保留旧窗口用量与策略，不续期其他对象或用途。需求用途按该对象的开发/审核配额建立后继窗口；完整 Parent 集成用途只续期已耗尽的 Development 或 Reviewer 配额，另一项未耗尽用量保留；Ticket 与 Parent-only 集成用途保留累计用量并追加额度。CI 续期只开启对应 CI 开发窗口，Reviewer 次数保持累计，Ticket CI 审核策略与两种 Parent 必须 Review 的规则不变；新提交、失败版本或 PR 更新不自行重置累计额度。resync 额度独立处理。只有明确的开发、审核或 CI 预算耗尽门禁允许 Resume 在该对象已存策略上接受本次 Delivery Policy 覆盖，不重新读取个人默认；普通执行续接不接受覆盖或开启预算窗口。它仍是同一 Job Generation，按创建时固定的 Development Thread Policy 选择开发 Thread，保留 branch、PR 和原工作，不把新 Attempt 伪装成旧窗口的额外轮次。
若检查点保留了尚需修改代码的准确失败证据，后继开发必须先把该证据交给按 Development Thread Policy 选择的开发会话，产生新 Candidate 后才启动新窗口的 Reviewer；真实 CI 失败通过独立 CI 开发用途处理，不先审查未修改且已知 CI 失败的 Candidate。复用仍有效 Thread 时由 Controller 根据 Development、Repair、Reviewer 或 Publication 角色选择短 Prompt，只重发完成本轮所需的当前动态证据；模型不接收 Resume、`execution_failed`、预算窗口或状态流转说明，Reviewer 与定向 Repair 只保留当前职责所需的简短审查次数。策略为新开发轮选择新 Thread 或显式 `--new-thread` 时，以相应角色和任务模式的完整标准 Prompt 新开 Thread；同轮故障缺少可恢复 Thread 时保留明确失败，不能自动替换。Resume 成功与否不改变 Job Generation；原需求或 currentness 已 stale 时不启动 Codex，按该对象的 Requeue 或重新验收边界处理，不以恢复消息接受正式需求或任务图变更。JSON 格式修复步骤因执行异常中断后，默认同 Thread Resume 只续接该次只读修复并继承已用次数，不重新开放开发任务。人工 Resume 不重置同一 Semantic Agent Attempt 已用的普通异常自动恢复额度；本次进程异常恢复不改变格式修复额度耗尽后的既有授权规则。未知外部写入仍先沿原 intent/receipt 对账；Resume 或 message 不替代绑定准确 PR/head/base、适用验收和 Required Checks 的最终人工批准。
_Avoid_: Output Repair、Publisher/check 幂等恢复、隐式 Requeue

**Business Resume（业务恢复）**:
在明确的业务暂停点，由维护者补充所需回应或授予继续执行的许可；Human Blocker 恢复延续已获授权的工作，预算耗尽检查点的恢复则明确授权新的预算窗口。它不表示修复一个意外退出的 Codex 进程。
_Avoid_: Execution Recovery、把所有 Resume 都解释为新开预算窗口

**Execution Recovery（执行续接）**:
Codex 在已获授权的工作尚未完成时异常结束后，对原工作、Thread、权限与执行步骤的续接；它可以按规则自动进行，也可以由维护者触发，但不新增或重置业务预算、窗口与格式修复额度。业务暂停不会仅因底层 Codex 进程已经结束而变成执行异常。
_Avoid_: Business Resume、重新开发、预算续期、Output Repair

**Automatic Invocation Recovery（调用自动恢复）**:
Executor 仍存活且确认 Worker 异常结束后，为完成同一 Semantic Agent Attempt 而进行的同 Thread 续接：普通异常最多一次，准确模型容量不足可持续自动恢复且不消耗普通异常额度，不替代 Executor 消失后的人工恢复。续接保留原工作、权限、业务预算与格式修复次数，并获得与原调用等长的新执行时限；JSON 修复中断时只续接该次只读修复，不重开开发或增加格式修复额度。
_Avoid_: Output Repair、自动替代 Thread、Executor 自动重启、将容量恢复扩大为所有错误的无限重试

**Ticket Resume Command（Ticket 恢复命令）**:
维护者通过 `agent-run resume <parent-issue>` 恢复当前 failed 或 Human Blocker Invocation；通过 `--ticket <number>` 指定一个 Ticket 时，其他 Ticket 仍在执行不妨碍接受该局部恢复。恢复只解除目标 Ticket 的局部门禁，保留适用成果与预算记录；无工作并发名额时进入 Ticket Work Queue，名额可用且执行条件仍满足时自动继续，其他 Ticket 的工作、阻塞与预算保持原状。在
`modification_budget_exhausted` 边界，该显式命令会同时新建有编号的 Ticket Review Budget Window 与 Ticket Development Budget Window；CI 恢复额度保持独立。Ticket 用尽有效 Run Policy Snapshot 的 `N` 次 Reviewer Invocation 本身不产生 `review_budget_exhausted`；最后 Findings 已由后续 Development 处理出新 Candidate 时自动进入 Deterministic Ticket Fallback，没有普通 Development 名额处理 Findings 时则产生 `modification_budget_exhausted`。
若该边界保存了 Reviewer Finding、Git Integrity 或 Required Checks 失败证据，新窗口先启动普通 Development Attempt，并将准确原始证据交给按 Development Thread Policy 选择的开发会话；只有产出新 Candidate 后才由新窗口 Reviewer 1 建立基线。准确新 head 的 CI 失败进入独立 CI 开发工作，不审查未修改的失败 Candidate。
若 preflight
发现 Ticket Content Revision、Parent Revision 或其他需求身份已变化，Controller 不启动 Codex，也不
复用原 Ticket Job、Ticket Branch 或 Development Thread；它停在 `requeue_required`，维护者需执行
`agent-run requeue <parent-issue>` 创建新的 generation。内容未变化时 Resume 仍可复用当前 Invocation 的
Thread；它不重置既有窗口的消耗记录。
_Avoid_: 隐式 Requeue、无内容变化重试、Run Feedback Revision

**Progress Exhaustion（推进耗尽）**:
Delivery Run 已不存在可自动推进的 Ticket Job，但 Ticket Set 尚未全部完成的状态。它汇总 DAG dependency、triage、局部 Human Blocker、单 Ticket 执行失败、预算耗尽及自身 CI 监督超时；独立 Ticket 会先继续推进。仍有正在执行、等待自动监督或已获授权排队的工作时，不因另一 Ticket 暂停而判定推进耗尽。需求或图谱合同变化、明确全局停止及已证实的全 Run 共享授权失效等 Run 级门禁仍即时阻止整个 Run。局部人工门禁保留准确 Ticket 身份、原始证据与恢复动作，不能借 Progress Exhaustion 使其自动恢复。
_Avoid_: 把局部门禁升级为全局停止、把待集成或 CI pass 当完成、最终整体验收

## 策略与预算窗口

**Delivery Policy（交付策略）**:
操作者拥有的语义轮次与按角色 Invocation Deadline 规则，来源优先级为内置默认、用户级默认和单次命令覆盖；仓库内容不能自行扩大操作者的 Agent 成本或运行时长。Ticket、Parent-only 与 Run 继续复用同一预算记录、门禁和恢复机制，但分别用与自身时间线匹配的轮次推导策略：Ticket Review 轮数 `N` 推导 Development 上限 `N+1`，前 `N` 组 Development/Review 配对后保留最后一次无 Review 的 Development Fallback；Run Repair 轮数 `N` 推导 Development 上限 `N` 与包含初始验收的 Reviewer 上限 `N+1`；Parent-only 轮数 `N` 推导 Development 与 Reviewer 都为 `N`。内置默认分别为 Ticket Review 3、Run Repair 3、Parent-only 10。Invocation Deadline 的内置默认是 Development 5 小时、Review 2 小时、Publication 1 小时，并允许用户级默认和单次命令按角色覆盖。
_Avoid_: 独立配置出无法闭环的 Development/Review 组合、仓库级资源预算、动态读取的运行中策略、Agent 自选预算

**Run Policy Snapshot（运行策略快照）**:
Delivery Run 创建或维护者在预算耗尽检查点显式开启新预算窗口时解析并保存的准确 Delivery Policy；新窗口沿用该 Run 已保存的策略，只有本次命令明确覆盖的字段可以改变，不读取最新个人默认。新 Run 同时保存初始创建配置供创建动作回执对账，恢复 Task Control 不依赖个人配置文件。普通 Execution Failure、Human Blocker 或监督超时后的 Resume 只继续原 Semantic Agent Attempt 或原等待窗口，必须复用原 Policy Snapshot，不得改变预算或 Invocation Deadline。已开始及历史预算窗口不因外部默认配置或后继窗口变化而改变。本策略与 Invocation Deadline 采用一次明确的规范状态协议切换：缺少 Policy Snapshot 的旧 Run 不迁移、不兼容推进，也不根据旧 Attempt 或当前默认值反推策略；只有按新协议创建的 Run 可以继续生命周期命令。
_Avoid_: 每次命令重新读取默认值、普通恢复中途改预算、追溯改写已消耗预算、推断或迁移旧 Run、仓库隐式覆盖

**Review Budget Checkpoint（审查预算检查点）**:
Run 或 Parent-only 的需求与集成 Reviewer 用尽当前窗口额度且仍有可修复 Findings 时，暂停并保留 Candidate、PR、Thread、证据与旧窗口历史。维护者显式 resume 只续期该层级审查/需求修复窗口；可修复 CI 失败独立在 CI Recovery Budget 耗尽边界暂停与续期。Ticket 普通 Reviewer 名额耗尽后按确定性兜底条件处理，不把 CI 恢复混入此窗口。
_Avoid_: 自动续期、清零历史、Resume 后先审查未修改 Candidate

**Ticket Review Budget Window（Ticket 审查预算窗口）**:
同一 Ticket 需求开发窗口最多 `N` 次 Reviewer Invocation，初始 Development 后的 Reviewer 1 及后续普通 Finding/Git Integrity 修复后的 Candidate 使用该额度；默认 `N=3`。Reviewer 名额用尽且 Findings 已由后续 Development 修复出新 Candidate 时使用 Deterministic Ticket Fallback。准确 Required Checks 失败进入独立累计 Ticket CI Development 额度，CI Development 与其 Review Findings 修改共同计费；新失败 head 不重置。CI Review 默认关闭，显式开启后所有轮次一致使用独立的 CI Reviewer Artifact；普通 Ticket Review 用量不因此增加。
_Avoid_: CI 修复消耗普通 Review 额度、伪造 CI 修复 Reviewer pass、隐式续期

**Run Review Budget Window（Run 审查预算窗口）**:
同一 Run Acceptance Generation 内一次明确授权的、最多 `N+1` 次 Run Reviewer Invocation 与 `N` 次 Run Development 的审计单元；`N` 来自有效 Run Policy Snapshot 的 Run Repair 轮数，内置默认 `N=3`。初始 Run Reviewer 不依赖 Run Development；Git 集成 Development 与 Findings 修复共用本窗口累计开发用量；Git 检查失败可先继续开发，只有检查成功后才进入适用整体验收。每个新 Candidate 或新的准确预期合并结果接受完整 Run Acceptance 时消耗一次；Reviewer 已形成合法 Acceptance Artifact 后即消耗额度，即使随后因 Currentness Boundary 漂移而废弃该结论。Reviewer 内部工作、Output Repair、执行失败、等待和未启动 Reviewer 的确定性 currentness 检查不计数。每次 Reviewer 都调用 `code-review` skill。首次 Reviewer 按固定首次范围建立结论；后续 Reviewer 只额外获得上一轮 Acceptance Artifact 的完整原始内容及其 default base、Run head 与 expected merge tree identity，Prompt 要求优先核销上轮 Findings、审查当前 Repair delta 与直接回归，缺少具体风险依据时避免重复完整扫描，仅在固定 review_range 内自主选择验证深度，不以预算窗口或旧证据缺失扩大范围。Reviewer N+1 仍产生可修复 Finding 时进入 Review Budget Checkpoint，不使用 Ticket 的确定性集成兜底；维护者显式 `resume` 在 Currentness Boundary 仍有效时创建有编号的续期窗口，只补充耗尽项并保留另一项用量、原工作来源和当前 Repair；后继按现场先修复 Findings 或审核新组合；所有旧窗口的用量、Findings 与修复历史继续保留。该 Prompt 优化不改变 Run Acceptance、CI Failure Evidence、Repair Cycle、Publication 或合并状态机。Parent-only 复用相同的预算记录、门禁、Artifact 和 Resume 机制，只使用等量 Development/Reviewer 的配对推导策略，而不复用本窗口的 `N+1` Reviewer 拓扑。
窗口及其历史固定保存在完整 Parent 的 Run Acceptance；创建、完成或轮换 Repair Job 都不搬移、归还或复制可变预算。Repair Job 的开发与审核引用该额度，工作身份、进度与证据仍属于准确修复工作。CI Recovery 同样只在所属交付对象持久保存；重载不依赖内存别名重绑。完整 Parent 的 Repair resync 仍按准确修复来源与集成窗口保存和计费，与主线 resync 及其他预算分开。不同对象、用途、角色及窗口保持独立，计费与续期策略不变，见 [ADR 0024](../adr/0024-orchestration-responsibility-separation.md)。
_Avoid_: Ticket Review Budget Window、把 `N` 次 Development 与 `N+1` 次 Reviewer 混成一个计数、Reviewer N+1 失败后自动 Repair、隐式额外 Reviewer

**Ticket Development Budget（Ticket 开发预算）**:
一个 Ticket Job 的初始开发与普通 Reviewer Finding、Git Integrity 修复使用冻结策略的 Ticket 需求开发额度。准确 Required Checks 失败使用独立的 CI Recovery Budget，不扣该额度；等待和回读不扣额度；实际 CI 调查即使未产出候选也计入 CI Development 用量。有剩余额度的无代码调查形成 `ci_investigation_paused`，显式 resume 启动新的 CI 工作并计费一次，保留失败证据及其他额度；调查用尽 CI 额度则只续期该 CI 窗口。普通 Ticket Review 名额用尽后的确定性兜底仍只处理需求开发路径，不能伪装 CI 修复的验收授权。
_Avoid_: 把 CI 修复计入普通需求开发次数、隐式额外 Reviewer

**CI Recovery Budget（CI 恢复预算）**:
Ticket CI 失败准确绑定当前发布 head 和检查后，交给同一开发—审核循环调查；不要求程序先认定为代码可修。pending、unknown 或尚未收敛的证据使用有界等待，无法开展工作时保留可继续暂停。`ticket_ci_review` 默认关闭，开启则所有修复轮次一致审核；免审不形成 Acceptance。`ticket_ci_development_limit` 默认 3，为同一 Ticket 的 CI Development 与 Findings 修改共用累计额度，独立于初始开发和集成；新 head、提交或 PR 更新不重置。重复观察不扣费，同一语义工作续接不重复扣费；耗尽后显式 resume 仅续期 CI 窗口，保留历史。两种 Parent 使用同一累计实现，`parent_ci_development_limit` 默认 3，始终要求完整 Parent Review；修复与最终 PR 使用同一 Run 额度，Run Branch 更新不重置用量。见 [ADR 0023](../adr/0023-parent-ci-development-loop.md)；Ticket 合同见 [ADR 0021](../adr/0021-ticket-ci-development-loop.md)。
最终 Run PR 的发布检查与批准复查共用失败证据匹配、完整 PR 身份回读、CI 预算、修复排队及暂停规则，见 [ADR 0016](../adr/0016-parent-integration-delivery.md)。pending/unknown、失败明细与 Observation 不匹配进入有界等待；PR/head 身份漂移仍使验收失效。同一 Run 的后继修复和最终 PR 共用累计 CI 额度。修复使旧批准失效，新组合只有取得正式 Run Acceptance、必要检查与新批准后才可最终合并。
_Avoid_: 同一失败 head 重复计轮、把 CI 修复记作需求修复、无证据跳过 Reviewer
