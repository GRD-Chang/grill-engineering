---
status: accepted
---

# 两种 Parent 在交付边界接纳远端成果并验收最终组合

## 决定

多 Ticket Parent 等全部 Ticket 真正完成后，先集成远端 Run Branch 与默认分支，再进行首次完整 Run Acceptance。Parent-only 保留固定开发基线的 D10/R10、Reviewer 必过及无 fallback 条件；需求开发结束后集成远端 Parent Branch 与默认分支。原验收只有在准确 head、tree、base 和需求边界仍适用时复用。两条路径共用准确远端快照集成、冲突提交和 resync 计数，审查与发布仍由现有 Engine、Adapter、Publisher 和 StateStore 控制。

普通 Parent 集成窗口先审核一次，默认最多三次有明确问题的修复及复审；Parent-only 的集成审查窗口独立于需求开发预算。纯 Git 冲突解决免费但有界，不能无限重试。真实外部 source/target 追加在旧窗口完成后开启新窗口并消耗 resync 机会；自己的发布回读及修复不重置窗口。显式 resume 只续期实际耗尽的额度。Reviewer 接收准确当前对象、上轮结果、集成记录和冲突路径，优先核销问题与直接回归。

Publisher 保留已发布源分支的祖先关系，以准确远端 head 为 lease 追加提交并更新原 PR。新的组合、CI head 或批准依据失效时回到相应审核或暂停边界；最终批准仍绑定当前交付对象。新增持久状态使用新的 Delivery Policy 协议号，旧 Run 在外部写入前明确拒绝，不做自动迁移。

可修复的 Required Checks 失败由每个交付对象独立的 CI 恢复额度处理：默认外层三轮、每轮最多三次开发修复。Ticket 首轮只允许一次修复；本轮使用准确旧发布授权、失败检查证据和新 Candidate 绑定的窄发布收据，不伪造 Reviewer 通过。Ticket 后续轮次与两种 Parent 的 CI 修复均需 Reviewer。CI 额度耗尽只暂停并续期对应层，不改需求开发、Parent 集成或 resync 额度。运行状态分别展示各窗口用量。

## 理由与边界

Ticket 集成已有精确远端读取、机械合并和独立 resync 额度；Parent 共用这些底层事实，但各自保留 Review-first 与 Dev-first 的入口语义。Run Acceptance 原来的临时合并预览不能证明远端 Run Branch 已被接纳，Parent-only 原来的 base 漂移阻断也不能处理协作者追加。共用集成边界使审核对象与后续发布的 tree 一致，并让预算、暂停和恢复有单一事实来源。服务端最终 base 与 merge 之间的瞬时竞态仍由现有准确 head 检查承担，本决定不增加分布式锁或 merge queue。
