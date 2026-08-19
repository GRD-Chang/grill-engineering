# Candidate Run Acceptance 可提升为正式 Run Acceptance

<!-- status: accepted -->

Run Acceptance 的对象固定为默认分支准确 head `D` 与 Run Branch 准确 head `R` 的预期合并结果，并覆盖完整 Parent Spec。Run Repair 先对 repair Candidate `C` 执行同范围的 Candidate Run Acceptance；仅当 Candidate、实际合入后的 Run tree、`D`、Parent、Graph 与 Ticket Completion 都严格一致时，Controller 才将该结论提升为正式 Run Acceptance。这样不对同一代码树重复派 Reviewer；任何边界漂移则重新验收。

## Considered Options

- repair 通过后无条件再次 Run Acceptance：安全，但对相同 Parent 与相同合并树重复消费 reviewer。
- repair reviewer 通过后直接接受：不安全，因为 repair 结果可能尚未对应当前默认分支或实际 Run Branch tree。

## Consequences

默认分支前进且可合并时重新验收最新组合，不创建 Repair；发生 Git conflict 时，Publisher 依据 Agent 在独立 Integration-repair Worktree 中解决的结果创建保留 `D` 与 `R` 双亲关系的 Merge-resolution Candidate，不能 squash 到旧 Run head。promotion 后仍重新生成 Run PR Narrative、等待 Required Checks，并保留最终人工验收。
