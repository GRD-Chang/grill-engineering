# 顶层 Codex 使用 Thread 绑定的角色执行配置

Grill Engineer 为 Development、Review 与 Publication 三类顶层 Codex 保存可在 Delivery Run 期间修改的 Agent Execution Profile，并在创建 Thread 前将当时有效的 model、reasoning effort 与 Profile Revision 固定为不可变 Thread Execution Binding；同一 Thread 的后续 Invocation、Resume 与 Output Repair 始终复用该绑定，配置修改只影响后来创建的新 Thread。配置只约束 Controller 直接启动的顶层 Codex，不约束其内部 subagent。内置 `economy` 预设使用 `gpt-5.6-luna`/`xhigh` 开发与 `gpt-6-astra`/`low` 审核，内置 `premium` 预设使用 `gpt-5.6-sol`/`medium` 开发与 `gpt-5.6-sol`/`high` 审核；Publication 默认引用 Development，用户也可显式覆盖任一角色。选择预设时保存解析后的准确值，不让已有 Run 随预设定义变化，也不在模型不可用时静默降级。

可变的当前 Agent Execution Profile 使用独立于 Run 状态的每-Run 控制面、锁和原子写，避免前台 Supervisor 持有的旧 Run 快照覆盖并发配置修改。Controller 在启动新 Thread 前先持久化其 Binding；该写入是本 Thread 的配置选择截止点。每次顶层 Agent Invocation 保存并展示实际 role、Thread、model、reasoning effort 与绑定的 Profile Revision，`history` 保留每次启动事实。第一版不提供主动轮换 Development Thread、交互式模型选择器或旧 Run 迁移。

## Considered Options

- 继续继承用户级 Codex 配置：实现最少，但 Run 不可复现，且无法保证 Development 与 Review 使用不同能力档。
- 每次 Invocation 重新读取配置：修改生效更快，但会让同一 Thread 在连续开发、恢复或 Output Repair 中切换模型。
- 把可变 Profile 写入现有 Run JSON：少一个文件，但现有长生命周期锁与整对象旧快照回写会造成阻塞或 lost update；修复它需要扩大为状态并发重构。

## Consequences

- 用户可在当前 Codex 运行时调整未来新 Thread，而不会改变正在运行或随后 Resume 的既有 Thread。
- 同一 Ticket 的 Development/Repair 保持原 Thread 配置；下一个 Ticket、新 Parent-only/Run Repair Job 或显式 `resume --new-thread` 使用最新 Profile Revision。
- Profile Store 与 Run state 必须分别保持原子性，并在 Thread 启动前建立可审计的 Binding；自定义模型或 effort 不可用时调用明确失败，不自动回退。
