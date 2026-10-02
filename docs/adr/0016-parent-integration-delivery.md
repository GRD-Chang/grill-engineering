---
status: accepted
---

# 两种 Parent 在交付边界接纳远端成果并验收最终组合

## 决定

多 Ticket Parent 等全部 Ticket 真正完成后，先集成远端 Run Branch 与默认分支，再进行首次完整 Run Acceptance。Parent-only 保留固定开发基线的 D10/R10、Reviewer 必过及无 fallback 条件；需求开发结束后集成远端 Parent Branch 与默认分支。原验收只有在准确 head、tree、base 和需求边界仍适用时复用。两条路径共用准确远端快照集成、冲突提交和 resync 计数，审查与发布仍由现有 Engine、Adapter、Publisher 和 StateStore 控制。

普通 Parent 集成窗口先审核一次，默认最多三次有明确问题的修复及复审；Parent-only 的集成审查窗口独立于需求开发预算。纯 Git 冲突解决免费但有界，不能无限重试。真实外部 source/target 追加在旧窗口完成后开启新窗口并消耗 resync 机会；自己的发布回读及修复不重置窗口。显式 resume 只续期实际耗尽的额度。Reviewer 接收准确当前对象、上轮结果、集成记录和冲突路径，优先核销问题与直接回归。

Publisher 保留已发布源分支的祖先关系，以准确远端 head 为 lease 追加提交并更新原 PR。新的组合、CI head 或批准依据失效时回到相应审核或暂停边界；最终批准仍绑定当前交付对象。新增持久状态使用新的 Delivery Policy 协议号，旧 Run 在外部写入前明确拒绝，不做自动迁移。

两种 Parent 的公开批准发现上述追加时，按 [ADR 0012](0012-noncancellable-final-approval.md) 结束旧批准段，自动进入可停止更新。Run Driver 的 `RunOutcome.next_step` 在每次事实刷新后重新选择交付、验收或发布；不继续刷新前已失效的操作。Change Delivery 与 Final Run Publication 在丢失原批准时先返回生命周期收口，接着由同一 Executor 复用普通循环。完整 Parent 保留 review-first：已完成 Ticket 不重做，新组合先做完整 Run Acceptance；Run Repair Candidate 仅在准确 tree/base/需求等匹配时提升，仍有效的候选不重复审核。两种 Parent 都不因这次衔接获得 Ticket fallback 或首轮 CI 窄授权，需求、集成、CI 与 resync 预算保持独立。

Final Run 批准意图先绑定已验收版本，刷新后由 Publication 核对当前完整 PR 身份及准确 source/target。PR 与 ref 的非原子回读不一致时有界等待；只有追加关系已证实时记录更新、撤销旧 grant 并转交普通 Run Integration，不再通过批准专用 merge preview 排入另一条冲突修复路线。已完成的历史 Invocation 不承担新版本 currentness；待收口的 Reviewer 结果恢复时仍保留 reviewing 与原 Semantic Attempt，先核对并收口已返回结果。正式发布保留最近已发布 head，并允许已被当前验收覆盖的 source 追加；不因自身更新反复失效、重复审查。已有 merge intent 时，若目标分支已包含批准 Run 而 PR 仍回读为 OPEN，保留原 grant 与 intent 并有界等待；即使目标随后再次追加，也只核对原合并并补收尾。合并前最后回读才出现 MERGED 时同样保留对账事实。

最终 PR 批准复查发现可修复 CI 失败时同样结束旧批准段，继续可停止的既有 Run Repair；额度耗尽则按共同人工待办暂停。最终 PR 与内部 Repair PR 共享 Run CI 额度。

Parent-only 的 PR 快照与分支头读取并非原子操作。PR 仓库、分支或状态失配仍按原门禁处理；仅 head/base SHA 尚未一致时，复用有界 GitHub 收敛监督重读完整发布门禁，不能继续使用旧 PR 快照。读取一致后再检查祖先关系及更新授权；读间重试不消耗 resync，持续不一致按既有监督超时暂停，允许显式 resume 后重新核对。

可修复的 Required Checks 失败由每个交付对象独立的 CI 恢复额度处理：默认外层三轮、每轮最多三次开发修复。Ticket 首轮只允许一次修复；本轮使用准确旧发布授权、失败检查证据和新 Candidate 绑定的窄发布收据，不伪造 Reviewer 通过。Ticket 后续轮次与两种 Parent 的 CI 修复均需 Reviewer。CI 额度耗尽只暂停并续期对应层，不改需求开发、Parent 集成或 resync 额度。运行状态分别展示各窗口用量。

多 Ticket Parent 的逻辑 CI 交付对象是 Run：最终 Run PR、内部 Repair PR 与后继提交共用 Run Acceptance 持久化的 `ci_recovery`。活动 Repair Job 持有相同预算快照，完成后由 Run 继续持有；更换内部 PR 或显式恢复不创建另一份额度。CI 修复的次数只记入 CI 内层，普通修复用量从独立 `review_budget` 展示。CI 内层或外层耗尽时，Repair Cycle 一同标记暂停；显式续期只恢复对应 CI 窗口及活动 Cycle，不重置外层累计轮数。

最终 Run PR 的发布检查与批准复查由 `RunPublicationShared` 统一处理 Required Checks 的失败、等待与预算暂停：先保存准确 PR/head 的 Observation，再读取匹配的原始失败证据并回读完整 PR 身份，只有全部失败可修复时才观察 CI 轮次并排队 Run Repair。相同失败 head 重复观察不重复计轮；实际 Development 才扣内层修复次数。pending、unknown、证据未收敛或不可修复故障不进入代码修复，沿用有界外部监督；耗尽后的显式 resume 保留失败 head、原始证据与旧窗口历史，仅续期耗尽层。

共享决策不接管各入口的授权和正式写入。批准仍校验准确批准对象，修复前及合并前分别保留必要回读，正式 Git/GitHub 写入仍由 Publisher 执行。CI 修复使原批准失效，后继结果须经既有 Candidate Acceptance 提升或 fresh Run Acceptance、最终 PR 检查和新批准；局部 Candidate 通过不独立构成正式 Run Acceptance。Ticket 首轮 CI 窄授权与 Parent-only 独立交付语义保持不变。

## 理由与边界

Ticket 集成已有精确远端读取、机械合并和独立 resync 额度；Parent 共用这些底层事实，但各自保留 Review-first 与 Dev-first 的入口语义。Run Acceptance 原来的临时合并预览不能证明远端 Run Branch 已被接纳，Parent-only 原来的 base 漂移阻断也不能处理协作者追加。共用集成边界使审核对象与后续发布的 tree 一致，并让预算、暂停和恢复有单一事实来源。服务端最终 base 与 merge 之间的瞬时竞态仍由现有准确 head 检查承担，本决定不增加分布式锁或 merge queue。
