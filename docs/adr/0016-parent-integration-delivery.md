---
status: accepted
---

# 两种 Parent 在交付边界接纳远端成果并验收最终组合

## 当前修订：取消内部 Repair PR

[ADR 0025](0025-parent-repair-without-internal-pr.md) 完整 Parent 修复改由程序更新 Run Branch，并通过最终 PR 的 CI 与人工批准交付，取代内部 Repair PR 的独立交付要求。下文按当前合同同步；被 ADR 0021 明确取代的 Parent-only 历史条款保留标注。

## 后续取代：Parent-only 集成循环

[ADR 0021](0021-parent-only-integration-loop.md) 取代下文 Parent-only 独立集成审核窗口、免费冲突开发及 resync 重置问题修复额度的规则。Parent-only 的 Git 问题与集成 Findings 共用累计 Development 额度，初始完整审核必须通过，验收以完整身份核对复用。多 Ticket Parent 和 Run Repair 的未替代部分保持既有合同。

## 决定

多 Ticket Parent 等全部 Ticket 真正完成后，先集成远端 Run Branch 与默认分支，再进行首次完整 Run Acceptance。Parent-only 保留固定开发基线的 D10/R10、Reviewer 必过及无 fallback 条件；需求开发结束后集成远端 Parent Branch 与默认分支。原验收只有在准确 head、tree、base 和需求边界仍适用时复用。两条路径共用准确远端快照集成、冲突提交和 resync 计数，审查与发布仍由现有 Engine、Adapter、Publisher 和 StateStore 控制。

多 Ticket Parent 不新增初始开发，首次完整 Run Acceptance 不可跳过。完整 Run 的 Git 集成问题与整体验收 Findings 共用 `run_repair_rounds` 限制的累计 Development 额度（默认 3），Reviewer 额度默认 4；普通 Git 检查失败在可编辑且身份可核验的现场交给 Development，保留准确操作、原始输出、source/target、HEAD/MERGE_HEAD 和历史。Agent 完成后程序复做 Git 检查；失败再交回，不能以摘要代替操作成功。远端追加、Run Branch 更新或新 Candidate 不重置这两项用量。显式 resume 只续期耗尽的 Development 或 Reviewer 额度，归档旧用量并保留另一项用量、原始来源、Candidate、线程和现场；状态重读和同一 Semantic Attempt 续接不重复扣费。resync 仍独立限制远端快照变化。Parent-only 的需求与集成窗口保持既有独立责任，其免费冲突路径不在此切片改动。

完整 Parent 后续审核绑定需求、基线、head/tree、Ticket Set/Completion 与准确预期合并结果；相同 tree 不单独构成复用证据。首次审核建立完整基线，后续可复用有效验证证据，增量核验变化和直接回归，并给当前整体成果形成结论。集成 Development Prompt 指明完整 Run 范围和准确问题证据，不重开完成的 Ticket；新会话与人工回复保持本轮工作来源。

整体验收 Findings 与最终 CI 修复属于完整 Parent 自身开发、集成与审核。程序依据有效集成记录、准确 source lease、候选与预期合并结果、必要祖先关系和持久写入意图直接更新 Run Branch；丢失响应先读回准确 ref，再收尾，不重复写入。保留修复工作身份、受管 worktree、必要本地分支和历史，不创建内部 Repair PR，不调用内部 Publish、不等待内部 CI，也不因内部 PR 合并轮换 Job。Run Branch 可暂时包含未通过最终 CI 的修复；只有最终 Run PR 承担 Publish 文案、必要 CI、人工批准和最终复核。更新被权限或仓库规则拒绝时保全成果并暂停，人工处理后通过 resume 继续，不修改规则或恢复旧流程。

Publisher 保留已发布源分支的祖先关系，以准确远端 head 为 lease 追加提交并更新原 PR。新的组合、CI head 或批准依据失效时回到相应审核或暂停边界；最终批准仍绑定当前交付对象。新增持久状态使用新的 Delivery Policy 协议号，旧 Run 在外部写入前明确拒绝，不做自动迁移。

两种 Parent 的公开批准发现上述追加时，按 [ADR 0012](0012-noncancellable-final-approval.md) 结束旧批准段，自动进入可停止更新。Run Driver 的 `RunOutcome.next_step` 在每次事实刷新后重新选择交付、验收或发布；不继续刷新前已失效的操作。Change Delivery 与 Final Run Publication 在丢失原批准时先返回生命周期收口，接着由同一 Executor 复用普通循环。完整 Parent 保留 review-first：已完成 Ticket 不重做，新组合先做完整 Run Acceptance；Run Repair Candidate 仅在准确 tree/base/需求等匹配时提升，仍有效的候选不重复审核。两种 Parent 都不因这次衔接获得 Ticket fallback 或首轮 CI 窄授权，需求、集成、CI 与 resync 预算保持独立。

Final Run 批准意图先绑定已验收版本，刷新后由 Publication 核对当前完整 PR 身份及准确 source/target。PR 与 ref 的非原子回读不一致时有界等待；只有追加关系已证实时记录更新、撤销旧 grant 并转交普通 Run Integration，不再通过批准专用 merge preview 排入另一条冲突修复路线。已完成的历史 Invocation 不承担新版本 currentness；待收口的 Reviewer 结果恢复时仍保留 reviewing 与原 Semantic Attempt，先核对并收口已返回结果。正式发布保留最近已发布 head，并允许已被当前验收覆盖的 source 追加；不因自身更新反复失效、重复审查。已有 merge intent 时，若目标分支已包含批准 Run 而 PR 仍回读为 OPEN，保留原 grant 与 intent 并有界等待；即使目标随后再次追加，也只核对原合并并补收尾。合并前最后回读才出现 MERGED 时同样保留对账事实。

最终 PR 批准复查发现可修复 CI 失败时同样结束旧批准段，继续可停止的既有 Run Repair；额度耗尽则按共同人工待办暂停。最终 PR 与修复工作使用同一累计 Run CI 额度。

Parent-only 的 PR 快照与分支头读取并非原子操作。PR 仓库、分支或状态失配仍按原门禁处理；仅 head/base SHA 尚未一致时，复用有界 GitHub 收敛监督重读完整发布门禁，不能继续使用旧 PR 快照。读取一致后再检查祖先关系及更新授权；读间重试不消耗 resync，持续不一致按既有监督超时暂停，允许显式 resume 后重新核对。

Ticket CI 路径由 [ADR 0021](0021-ticket-ci-development-loop.md) 定义，两种 Parent 的 CI 定向开发与整体增量验收由 [ADR 0023](0023-parent-ci-development-loop.md) 定义；三种对象复用同一累计 CI 额度实现和开发循环，不保留旧可修性分类、首轮例外或内外层预算。Ticket 的可选审核配置不影响 Parent Review 与人工批准。

多 Ticket Parent 的 CI 交付对象是 Run：最终 Run PR、修复工作与后继提交共用 Run Acceptance 持久化的 `ci_recovery`。活动 Repair Job 使用同一对象的额度，完成后由 Run 继续持有；更换 PR、新失败 head 和目标追加不创建另一份额度。实际 CI Development 与 Findings 修改记入 `developments_used`，普通集成用量仍由独立预算展示。CI 额度耗尽时 Repair Cycle 一同暂停，显式 resume 只续期对应 CI 窗口并保留历史。

最终 Run PR 的发布检查与批准复查由 `RunPublicationShared` 统一处理 Required Checks 的失败、等待与预算暂停：先保存准确 PR/head 的 Observation，再读取匹配的原始失败证据并回读完整 PR 身份，然后交给定向 Run Repair。相同事实重复观察不扣费；实际新 Semantic Development 才消耗累计 CI 额度。pending、unknown 与证据未收敛沿用有界外部监督；无法完成调查时保留人工暂停与公开 resume。

共享决策不接管各入口的授权和正式写入。批准仍校验准确批准对象，修复前及合并前分别保留必要回读，正式 Git/GitHub 写入仍由 Publisher 执行。CI 修复使原批准失效，后继结果须经既有 Candidate Acceptance 提升或 fresh Run Acceptance、最终 PR 检查和新批准；局部 Candidate 通过不独立构成正式 Run Acceptance。Ticket CI 免审收据与 Parent-only 独立交付语义各自保持准确对象边界。

## 理由与边界

Ticket 集成已有精确远端读取、机械合并和独立 resync 额度；Parent 共用这些底层事实，但各自保留 Review-first 与 Dev-first 的入口语义。Run Acceptance 原来的临时合并预览不能证明远端 Run Branch 已被接纳，Parent-only 原来的 base 漂移阻断也不能处理协作者追加。共用集成边界使审核对象与后续发布的 tree 一致，并让预算、暂停和恢复有单一事实来源。服务端最终 base 与 merge 之间的瞬时竞态仍由现有准确 head 检查承担，本决定不增加分布式锁或 merge queue。
