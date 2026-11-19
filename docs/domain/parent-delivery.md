# Parent 整体交付

Parent-only 与完整 Parent 的整体验收、修复和人工批准。操作入口见[运行说明中的 Parent 交付](../agent-run.md#命令边界与状态轮转)，修复 PR 的决策与授权见 [ADR 0031](../adr/0031-parent-repair-pr-and-repository-ci.md)。

[按任务查阅术语与合同](../../GLOSSARY-MAP.md)

## 两种 Parent 交付

**Run Branch（运行分支）**:
一个 Delivery Run 独有、由 Controller 创建和维护的受管 Run Branch。已通过 Ticket Integration Gate（Fresh Acceptance 或 Deterministic Ticket Fallback）的 Ticket 变更先进入该临时集成分支，整个 Delivery Run 最终通过它接受人工整体验收后才进入默认分支。
_Avoid_: 默认分支、Ticket Branch、永久集成分支

**Parent-only Delivery（仅 Parent 交付）**:
当 Parent Issue 没有任何 Child Ticket 时使用的轻量完整交付：Publisher 创建与 Parent Issue 原生关联的唯一 Parent Branch 与直达默认分支的 Parent PR。它仍遵循共享的 Candidate-first、Fresh Acceptance、Required Checks 和 Published-Head Gate；每个预算窗口按有效 Run Policy Snapshot 的 Parent-only 轮数 `N` 允许最多 `N` 次 Development 与 `N` 次 Reviewer Invocation，并严格组成 `Development N → Reviewer N` 的完整配对，内置默认 `N=10`。每次 Reviewer 都调用 `code-review` skill；首次 Reviewer 按固定首次范围建立 Parent 结论，后续 Reviewer 只额外获得上一轮 Acceptance Artifact 的完整原始内容及其 reviewed base/Candidate identity，优先核销原 Findings、检查 repair delta 与直接回归，缺少具体风险依据时避免重复完整扫描，在固定 review_range 内自主选择验证深度；Reviewer N 仍失败时进入 Review Budget Checkpoint。Parent-only 直接面向默认分支且没有后继 Run Acceptance，因此不允许 Deterministic Ticket Fallback，也不允许任何未经当前 Reviewer 验收的新 Candidate 进入 Publication。`agent-run approve <parent-issue>` 只授予一次合并权限，Development–Acceptance Engine 随后重新核对当前 Parent Revision、验收记录、检查、默认分支与 PR head，并通过 Publisher 以普通 merge 合并。已合并但 closeout 响应丢失时，共享 Engine 只重试幂等审计评论和关闭，不得再次合并。
_Avoid_: Run Branch、Final Run PR、跳过独立验收、自动合并

**Run PR（运行拉取请求）**:
全部 Ticket Completion 且准确 Run Branch head 通过 Run Acceptance 后，才由 Run Branch 指向默认分支创建或刷新正文的最终集成 PR。它接受 Hosted CI，通过后交给维护者进行整个 Delivery Run 的最终人工验收；人工批准后使用普通 merge commit 和 `--match-head-commit` 合入默认分支，从而保留每张 Ticket 的 squash commit，并形成一个可整体回滚的 Run 边界。
_Avoid_: Ticket PR、squash 整个 Delivery Run、自动合入默认分支

**Run Acceptance（运行整体验收）**:
全部 Ticket Completion 后，对准确默认分支 head `D` 与 Run Branch head `R` 的预期合并结果执行的独立整体验收。该结果必须可合并并满足完整 Parent Spec。它为最终 Run PR 的发布和人工批准提供整体验收证据。

Reviewer 检查 Parent Spec、Run Feedback Revisions、完整 Ticket Set、`D`、`R`、预期合并结果、累计 diff 与各 Ticket 集成证据。覆盖跨 Ticket 交互、需求遗漏、累计偏离和集成回归，以及 Deterministic Integration Record 中的原 Reviewer 产物与后续开发事实。新一轮使用独立 Reviewer Thread 和一次性 Validation Checkout。首轮、后续范围与上轮原始证据见[独立验收合同](../agents/agent-prompts.md#首次验收与上次结果)。同一 Reviewer 的执行或人工续接按[恢复合同](recovery.md#恢复意图与-invocation-续接)处理，输出修复按[调用合同](worker.md#attempt-与-invocation)处理。

`D`、`R` 或有效需求 Revision 变化时按当前组合重新核验适用性，生成或刷新 Run PR Narrative 需要当前通过。审查、修复额度和耗尽续期统一见[策略与预算窗口](recovery.md#策略与预算窗口)。
_Avoid_: Ticket Fresh Acceptance、简单汇总各票 pass、最终人工验收、只审 Repair diff

**Candidate Run Acceptance（候选运行整体验收）**:
Run Repair 尚未写入 Run Branch 时，对 repair Candidate `C` 与准确默认分支 head `D` 的预期合并结果执行的完整 Run Acceptance。它与正常 Run Acceptance 使用相同的 Parent 范围和 E2E、Standards、Spec lane；每个 Candidate 的 tree、预期合并 tree、requirements/currentness 边界、Reviewer 与结论形成不可变的轻量审计记录，由所属 Run 永久保留而不截断。其通过记录只有在 Candidate、Repair base、实际合入后的 Run Branch tree、默认分支、Parent Spec、Ticket Graph 与 Ticket Completion 全部严格一致时，才可由 Controller 提升为正式 Run Acceptance。否则该记录失效，不得复用。
_Avoid_: 仅审 repair diff、合入后无条件重跑同一 Reviewer、用候选通过直接授权发布

**Default Branch Drift（默认分支漂移）**:
默认分支 head 发生变化，使最终预期合并结果不再是 Reviewer 验收过的组合。Controller 自动废弃旧 Run Acceptance 与 Run PR Narrative，并首先在新 base 和当前 Run Branch 或 repair Candidate 上重新预演合并：预演成功时直接对该新组合执行全新整体验收，不因漂移直接开始代码修复；预演冲突或新组合验收失败时，才把原始证据交给 Run Repair Codex。活跃 Repair 中再次发生默认分支漂移时，Controller 固化已有 Candidate、保留 Repair Thread 与物理 Integration-repair Worktree，并按新 base 重新准备集成现场；Run Branch、Parent、Graph 或 Ticket Completion 漂移则使 Repair Job Generation 失效，不自动迁移其 Candidate 或工作区。只有无法自动解决或耗尽预算时才请求人工介入。
_Avoid_: 把 base 漂移直接当作代码修复、将旧 repair 自动套用到新的交付边界、Ticket Content Revision、人工逐次确认、复用旧验收

## 发布与修复候选

**Run Publication Codex（运行发布 Codex）**:
Run Acceptance 通过后由 Controller 启动的只读 YOLO Codex，读取 Parent Spec、完整 Ticket Set、各 Ticket PR、准确累计 diff 与真实验证证据，生成符合统一 PR Narrative 的 Run PR title/body。正常 Run Publication Attempt 使用新的 Codex Thread；失败或 Human Blocker 只能通过显式 `resume` 继续同一 Thread，或用 `--new-thread` 新开 Thread。同一角色继续时只接收 Publication 角色短 Prompt 与当前必要发布证据，新 Thread 接收完整标准 Prompt；完整 flat contract 输出不合法时最多进行两次同 Thread、只读、只重发合法 Publication wire JSON 的格式修复。它的职责只包含发布语义，不复用 Reviewer Thread、不执行验收、不整理 Candidate checkout；若自己创建 checkout 外临时路径，负责在完成前按 Delivery Hygiene 清理。Run Branch、默认分支 base、有效需求或证据变化后必须基于新状态重新生成。
_Avoid_: Run Acceptance Reviewer、Controller 拼接正文、Run Repair Thread

**Run Repair Thread（运行修复线程）**:
为 Delivery Run 最终集成修复服务的 Development Thread，其跨开发轮的会话关系同样遵循 Development Thread Policy。它面对完整 Parent 范围、当前集成代码和准确修复证据，不复用任何 Ticket Development Thread 或 Reviewer Thread。
_Avoid_: Ticket Development Thread、Run Reviewer Thread、人工修复会话

**Integration-repair Worktree（集成修复工作区）**:
完整 Parent 修复所使用的持久、可写受管工作区，与 Run Branch 和 Reviewer 的一次性 Validation Checkout 分离；它保存当前修复的未提交成果及必要集成现场，供同一工作的后续修改或恢复继续使用。Development 负责编辑和验证，正式提交及发布由程序负责。
_Avoid_: Run Branch 本身、Reviewer 的只读工作区、Agent 自行发布、每次 Finding 都丢弃原修复现场

**Run Repair Job（运行修复任务）**:
完整 Parent 中承接整体验收 Findings、CI 失败或集成问题的一份修复工作，保留明确工作身份、原始证据、成果与恢复位置。它使用 Parent 的额度与交付循环；Run Repair PR 承载修复成果进入 Run Branch 的检查与合入，不形成另一份 Parent 工作或预算，见 [ADR 0031](../adr/0031-parent-repair-pr-and-repository-ci.md)。

程序通过 Run Repair PR 将审核通过且绑定事实仍有效的准确成果合入 Run Branch，不因修复 PR 合并而轮换 Job 或重置预算；已合并 PR 作为原成果记录，不复用为后续新修复的活动 PR。默认分支前进后在原修复工作中核验最新组合，保存重载和 resume 保留工作身份与已保存结果。
_Avoid_: Ticket Job、独立修复流水线、另有一份 Parent 预算

**Repair Cycle（修复周期）**:
为解决同一准确 Run Acceptance、Run PR CI 或集成问题而连续执行的有界工作；一个 Cycle 保持当前 Run Repair Job 的工作身份，任一时刻只允许一个活跃修复工作；默认分支前进或准确 Run Branch 写入不产生交付轮换。集成 Git 问题与整体 Findings 使用独立的累计集成额度；准确 CI 失败的 Development 与 Review Findings 修改使用同一 Parent 的累计 CI Recovery Budget。状态观察、机械 Git 检查和 Required Checks 重读不扣 Agent 工作额度；实际新 Development 调查即使无代码候选也计费，同一语义工作的中断续接不重复计费。CI 失败绑定准确 PR/head 和 Observation 后携检查事实与可取得日志交给 Development，不使用 code-failure-steps 白名单或可修性分类；pending、unknown 与未匹配证据进入有界监督。默认分支追加、Run Branch 更新、新失败版本和 revalidation 不自动重置额度。Candidate 提升、人工暂停与预算耗尽保存 Cycle 历史；公开 resume 仅续期实际耗尽项，最终仍需当前整体验收、CI 和人工批准。
_Avoid_: 用 Delivery Run 的历史累计修改数作为当前预算、把流程重试计作代码修改、base 漂移重置同一问题的预算

**Run Repair PR（运行修复拉取请求）**:
以 Run Branch 为目标，承载完整 Parent 的修复或整体集成成果的拉取请求。候选验收、目标分支检查、程序合入与最终批准的边界由 [ADR 0031](../adr/0031-parent-repair-pr-and-repository-ci.md) 定义。未合入 PR 在适用的普通返工中原地更新，已合入 PR 保留为原结果。
_Avoid_: 最终 Run PR、Ticket PR、Parent-only PR、直接推送 Run Branch

**Merge-resolution Candidate（合并解决候选）**:
程序依据修复工作区生成、记录准确 Run head 与默认分支 head 为父提交的冲突解决成果，是完整 Parent 审核的候选对象。它保留已解决的默认分支祖先关系；实际发布成果仍须与被审核的文件树和集成事实一致，才能沿用审核结论。
_Avoid_: Agent 自行提交 merge、squash 丢失集成祖先、把冲突只当作文本补丁

**Run Acceptance Repair Loop（运行验收修复循环）**:
完整 Parent 为解决整体验收 Findings 或最终 PR 的 CI 失败而进行的有界修复，复用开发—审核与集成—Git 检查—适用审核两个循环；修复始终覆盖当前 Parent 的工作范围。按 [ADR 0031](../adr/0031-parent-repair-pr-and-repository-ci.md)，程序经 Run Repair PR 的检查与合入更新 Run Branch，随后由最终 PR 的检查和人工批准继续把关，保留成果、证据和累计用量供后续修复或 resume。
_Avoid_: 另一套修复预算或审核流水线、仅修复局部 Ticket、无依据复用旧审核、Agent 直接更新 Run Branch

**Run Feedback Revision（运行反馈版本）**:
维护者通过 Final Revision Command 提交的 Run 级权威反馈及其稳定指纹。它进入 Run Repair Job 的 Effective Revision、Run Acceptance Artifact envelope 和后续 Run Acceptance 需求源，直到 Delivery Run 被批准或放弃；Controller 不总结、改写或静默丢弃该反馈。
_Avoid_: PR 普通评论、Parent Spec Revision、Controller 摘要

## 人工批准与收尾

**Final Human Acceptance（最终人工验收）**:
Run Acceptance 与 Run PR Required Checks 全部通过后，Controller 首次向维护者请求的 Delivery Run 整体确认。维护者通过本地 `agent-run approve <parent-issue>` 显式批准；Controller 随后重新核对默认分支 live base、Run PR live head、Run Acceptance、Required Checks、Parent Spec Revision 与 Ticket Graph Revision，完全一致时 Publisher 才可使用普通 merge commit 和 `--match-head-commit` 合入默认分支。批准只绑定这一准确 head/base 与验收结果：同一 head/base 的 GitHub 收敛延迟由 Controller 自动监督，新 head 或 base 则使批准失效并要求新的最终人工验收。此前各 Ticket 的自动完成不触发逐票人工验收。
_Avoid_: Ticket 级确认、自动 merge 默认分支、Agent 语义范围判断

**Final Approval Command（最终批准命令）**:
维护者通过本地显式命令 `agent-run approve <parent-issue>`，对已通过全部自动门禁的 Delivery Run 发起的不可取消的最终交付操作，涵盖最终条件复核、合并与收尾；接受后不允许操作者用 stop 撤销正在执行的操作，但可以因错误结束，并由显式 `resume` 在确认原执行已结束及实际结果后继续未完成部分。该授权只对重新验证的 Run PR head、base 和有效 Revision 生效，事实未变时恢复原操作无需重复批准，新 head 或 base 则令授权失效并重新请求验收；失败不使已合并代码自动回滚，结果未知时必须先确认是否已生效。命令等待完整操作完成或明确无法继续后返回；后台模式关闭终端只离开观察，不取消操作；前台不保证会话结束后持续，但不撤销批准，未知结果仍须按原意图读回。准入、回执与恢复边界见 [ADR 0012](../adr/0012-noncancellable-final-approval.md)。
_Avoid_: GitHub Approve、标签触发、永久授权、可取消操作、失败时全部回滚、恢复时重复合并、以未知结果当作未执行

**Final Revision Command（最终修改命令）**:
维护者在完整 Parent 最终批准阶段通过 `revise --message` 提交 Run 级反馈的命令。只有当前证据与门禁允许时方可应用。反馈原样形成 Run Feedback Revision 并进入修复，共用候选验收与提升规则。具体准入和预算见[命令边界](../agent-run.md#命令边界与状态轮转)。Ticket Set 或依赖变化按 Unsupported Scope Change 处理。
_Avoid_: Parent Spec 静默改写、直接修改 Run Branch、无限自动重试

**Final Abandon Command（最终放弃命令）**:
维护者通过 `agent-run abandon <parent-issue>` 永久终止尚未进入默认分支的 Delivery Run 的显式命令。Controller 执行 Run Abandonment Recovery、关闭或标记未合并的自动化 PR，并保留完整审计证据；暂时等待或普通修复不能使用该命令。若任一 Managed Development Checkout 仍含 tracked modifications 或 untracked files，普通 `abandon` 必须在任何外部写入或删除前拒绝执行并报告准确路径；只有维护者额外显式给出 `--discard-worktree` 才授权不可恢复地删除这些未提交成果后继续放弃。
_Avoid_: 暂停、单 Ticket 阻塞、默认分支回滚、将 `abandon` 默认解释为丢弃未提交成果

**Run Merge Commit（运行合并提交）**:
Run PR 通过最终人工整体验收后写入默认分支的普通 merge commit。其第二父历史保留 Run Branch 中各 Ticket squash commits，使系统既能按 Ticket 回滚，也能按整次 Delivery Run 回滚。
_Avoid_: Ticket Squash Merge、fast-forward、再次 squash 全部 Tickets

**Run Abandonment Recovery（运行放弃恢复）**:
Delivery Run 在进入默认分支前被人工取消或永久放弃时，由 Publisher 只重新打开该 Run 曾记录为由 Publisher 从 open 变为 closed、且尚未进入默认分支的 Tickets，并留下恢复原因和 Run 证据；运行开始前已关闭或由外部主体独立关闭的 Issue 不得被重开。普通返工或暂时阻塞不触发整批重开。
_Avoid_: Ticket 自动修复、Run 暂停、默认分支回滚

## Parent 集成的补充边界

Parent 两条路径采用[远端快照与 resync 规则](../adr/0016-parent-integration-delivery.md)，
复用 [ADR 0015](../adr/0015-ticket-remote-integration.md) 的自动额度口径。首次机械集成
不计入 resync。同一快照重试、程序发布与回读、Reviewer 退回及 CI 失败保留原窗口；
完成后接纳真实外部新增提交才开启后继窗口。多 Ticket Parent 的累计开发和审核额度，
以及 Parent-only 的累计集成开发额度均独立保留。额度耗尽时，`resume` 只续期对应 resync 额度。
Parent-only 的初始开发和集成合同分别见 [ADR 0016](../adr/0016-parent-integration-delivery.md)
与 [ADR 0021](../adr/0021-parent-only-integration-loop.md)，CI 见 [ADR 0023](../adr/0023-parent-ci-development-loop.md)。

集成记录按准确的 source、target、Candidate、结果 head/tree 和冲突路径核验。
已发布源分支按追加历史更新和复用原 PR；非追加变化进入人工处理。候选或集成事实
变更后按当前结果核验 Review、Required Checks 和最终人工批准。
开发修复保留原 Work Subject、Generation、Semantic Attempt、适用线程、
预算窗口和冲突证据。同轮中断恢复沿用原计费，后续新修复按所属用途计费。
已收尾历史保留准确原结果，通过事实关联读取，无需活动 Attempt 或冲突工作区。
恢复边界见[恢复与预算](recovery.md)与[调用生命周期](../agents/attempt-lifecycle.md)。
